"""choose — the ``choose_one`` primitive: pick ONE from a group of equivalent
options, with an optional content-free hint (#125).

The generalisation of :class:`~zu_tools.selection.FirstOptionSelectionSatisfier`
(#95/#120). Where the satisfier sets the first valid option of EVERY required variant
group (colour AND size), ``choose_one`` makes ONE choice from ONE group — the option a
content-free HINT names — over ANY group of equivalent options:

  * a product-variant swatch / radio group, or a native ``<select>`` (shopping),
  * a calendar DAY grid or a TIME grid (appointment booking),
  * a search-RESULT list or a SERVICE list (the discovery prefix that broke the audit).

It is the single 'choose from a group (+ hint)' call that unifies select-variant,
pick-slot, pick-service and pick-search-result — the same primitive a booking funnel's
discovery step needs and a shopping funnel's variant step needs. With NO hint it falls
back to satisfying the required variant groups (delegating to the shipped satisfier, so
the shopping behaviour is byte-identical), so ``choose_one`` subsumes both.

Content-free throughout. A GROUP is recognised by STRUCTURE — a shared structural
``group`` id, or a homogeneous run of one selectable role — never by option prose. The
HINT resolves by POSITION ('earliest' / 'first' / 'last') or by matching a token against
option NAMES (names are DATA the primitive matches, never instructions it obeys —
injection-immune). The success invariant, checked over the :class:`ConnectedSurface`:
the chosen option became SELECTED, or the surface ADVANCED (a slot / result click
navigates to the next step) — a silent no-op is reported as such, never a false success.
"""

from __future__ import annotations

import re

from zu_core.ports import (
    ConnectedSurface,
    PrimitiveOutcome,
    PrimitivePlan,
    SurfaceAction,
)
from zu_core.surface import SurfaceAffordance, SurfaceView

from ._commerce import is_commit
from .selection import FirstOptionSelectionSatisfier

# STRONG option roles — a control that is UNAMBIGUOUSLY a single-choice option (a
# variant swatch/radio, a listbox option, a tab, a calendar gridcell). Anything already
# carrying a selected-style state also counts (a styled ``div[role=button]`` swatch).
_STRONG_ROLES: frozenset[str] = frozenset(
    {"radio", "option", "tab", "swatch", "menuitemradio", "menuitemcheckbox", "gridcell"}
)
# WEAK option roles — a control that is an option only in CONTEXT: a homogeneous run of
# two or more forms a choice group (a slot grid rendered as buttons, a result/service
# list rendered as links/list items). Used for token resolution across the whole surface
# and, when no strong group exists, for positional resolution.
_WEAK_ROLES: frozenset[str] = frozenset({"button", "link", "menuitem", "listitem"})
# A native single-choice ``<select>`` reduces to role 'combobox' — driven with the
# ``select`` verb (browser-side option picker), matching the hint against its options.
_COMBOBOX = "combobox"
# The state tokens that read as 'this option is the chosen one' — kept in step with
# zu_patterns' SELECTED_STATES and selection.py's set (a drift test enforces alignment).
_SELECTED_STATES: frozenset[str] = frozenset(
    {"selected", "checked", "pressed", "active", "aria-selected"}
)
_UNAVAILABLE: frozenset[str] = frozenset({"disabled", "unavailable", "hidden"})

# Positional hints — a content-free NUDGE naming a slot in page order, not any prose.
_FIRST_HINTS: frozenset[str] = frozenset(
    {
        "first", "earliest", "soonest", "top", "1", "asap", "first available",
        "earliest available", "first-available", "earliest-available", "any", "next",
    }
)
_LAST_HINTS: frozenset[str] = frozenset({"last", "latest", "newest", "bottom", "end"})


def _norm_states(a: SurfaceAffordance) -> set[str]:
    # states may arrive as 'selected' or 'selected:true' — key off the token.
    return {s.split(":", 1)[0].lower() for s in a.states}


def _is_selected(a: SurfaceAffordance) -> bool:
    return bool(_norm_states(a) & _SELECTED_STATES)


def _enabled(a: SurfaceAffordance) -> bool:
    return not (_norm_states(a) & _UNAVAILABLE)


def _ident(a: SurfaceAffordance) -> tuple[str, str]:
    """A control's render-STABLE identity (role, label) — handles renumber after a
    click that re-renders, so identity is what re-finds the same option in ``after``."""
    return (a.role.lower(), a.label.strip().lower())


def _comboboxes(view: SurfaceView) -> list[SurfaceAffordance]:
    return [
        a for a in view.affordances if a.role.lower() == _COMBOBOX and _enabled(a)
    ]


def _strong_groups(view: SurfaceView) -> list[list[SurfaceAffordance]]:
    """Groups of STRONG option controls: options sharing a structural ``group`` id, else
    a maximal contiguous run of one strong role. Each returned group has >= 2 options —
    a genuine CHOICE (a lone option is not a picker)."""
    opts = [a for a in view.affordances if a.role.lower() in _STRONG_ROLES or _is_selected(a)]
    return _partition(opts)


def _weak_runs(view: SurfaceView) -> list[list[SurfaceAffordance]]:
    """Homogeneous runs (>= 2) of a WEAK option role — a slot grid of buttons, a result
    list of links/list items. Only contiguous same-role runs count, so an incidental pair
    of unrelated buttons across the page is not a group."""
    opts = [a for a in view.affordances if a.role.lower() in _WEAK_ROLES]
    return _partition(opts)


def _partition(opts: list[SurfaceAffordance]) -> list[list[SurfaceAffordance]]:
    """Partition ``opts`` into single-choice groups: options with a shared structural
    ``group`` id are one group; the rest fall back to maximal contiguous runs of the same
    role. Only groups with >= 2 options are kept (a group is a CHOICE)."""
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
        if run and a.role.lower() == run[-1].role.lower():
            run.append(a)
        else:
            if run:
                groups.append(run)
            run = [a]
    if run:
        groups.append(run)
    return [g for g in groups if len(g) >= 2]


# NAV CHROME interleaved with options — a modal lists 'Go back' / 'Close' alongside the real
# choices, and a positional 'first' would otherwise pick the chrome and back OUT of the step. So
# chrome is never a candidate. Whole-word, content-free.
_CHROME_RE = re.compile(
    r"\b(close|cancel|dismiss|back|go\s*back|previous|prev|skip|not\s*now|no\s*thanks)\b",
    re.IGNORECASE,
)


def _enabled_opts(group: list[SurfaceAffordance]) -> list[SurfaceAffordance]:
    # A choosable option is enabled, addressable, NOT a committing control (choose_one must not pick
    # a pay / place-order / confirm control — the host's approval boundary), and NOT nav chrome (a
    # close/back/skip that would abandon the step). Neither is a candidate.
    return [a for a in group if _enabled(a) and a.handle
            and not is_commit(a.label) and not _CHROME_RE.search(a.label or "")]


def _target_group(view: SurfaceView) -> list[SurfaceAffordance] | None:
    """The group a POSITIONAL hint targets: prefer a STRONG group (unambiguous options),
    else the LARGEST weak run. Ties broken by page order (the earliest group). ``None``
    when the surface presents no choice group at all — the primitive is then inert."""
    strong = [g for g in _strong_groups(view) if _enabled_opts(g)]
    if strong:
        return max(strong, key=lambda g: len(_enabled_opts(g)))
    weak = [g for g in _weak_runs(view) if _enabled_opts(g)]
    if weak:
        return max(weak, key=lambda g: len(_enabled_opts(g)))
    return None


def _all_options(view: SurfaceView) -> list[SurfaceAffordance]:
    """Every enabled option across strong groups and weak runs, in page order — the
    search space a TOKEN hint matches against."""
    seen: set[str] = set()
    out: list[SurfaceAffordance] = []
    for g in (*_strong_groups(view), *_weak_runs(view)):
        for a in _enabled_opts(g):
            if a.handle not in seen:
                seen.add(a.handle)
                out.append(a)
    # keep page order
    order = {a.handle: i for i, a in enumerate(view.affordances)}
    return sorted(out, key=lambda a: order.get(a.handle, 0))


def _searchable_name(a: SurfaceAffordance, collides: bool) -> str:
    """The name a token matches against: the option's own label, plus its ENCLOSING
    label (the card/section heading) WHEN the label is a genuine collision (a row of
    identical 'Select' buttons) or is empty — so an option addressable only by its card
    heading becomes selectable, without broadening a normal distinctive name (#127)."""
    if a.enclosing_label and (collides or not a.label.strip()):
        return f"{a.label} {a.enclosing_label}".strip()
    return a.label


def _match_token(view: SurfaceView, token: str) -> SurfaceAffordance | None:
    """The first enabled option whose NAME matches ``token`` on WORD BOUNDARIES — so
    'haircut' matches 'Haircut — 30 min', '9:30' matches '9:30 AM' but NOT '19:30', and
    'monday' matches 'Monday 6 Jul'. On a genuine name COLLISION (a list of identically
    named 'Select' buttons), the option's ENCLOSING card label is folded in so the hint
    resolves the right card (#127). Content-free: the name is data, never obeyed."""
    tok = token.strip()
    if not tok:
        return None
    pat = re.compile(r"\b" + re.escape(tok) + r"\b", re.IGNORECASE)
    opts = _all_options(view)
    counts: dict[str, int] = {}
    for a in opts:
        key = a.label.strip().lower()
        counts[key] = counts.get(key, 0) + 1
    for a in opts:
        collides = counts.get(a.label.strip().lower(), 0) > 1
        if pat.search(_searchable_name(a, collides)):
            return a
    return None


def resolve(view: SurfaceView, hint: str | None) -> SurfaceAffordance | None:
    """The option a ``hint`` names, or ``None``. POSITIONAL hints ('earliest'/'first'/
    'last') pick from the target group by page position; any other hint is a TOKEN
    matched against option names. ``None`` hint returns ``None`` here (the unhinted
    variant path is handled by the primitive, delegating to the satisfier)."""
    if hint is None:
        return None
    h = hint.strip().lower()
    if h in _FIRST_HINTS or h in _LAST_HINTS:
        group = _target_group(view)
        if not group:
            return None
        opts = _enabled_opts(group)
        if not opts:
            return None
        return opts[-1] if h in _LAST_HINTS else opts[0]
    return _match_token(view, h)


def _verify(before: SurfaceView, after: SurfaceView, target: SurfaceAffordance) -> bool:
    """The choose_one success invariant: the chosen option became SELECTED (re-found by
    identity across a re-render), OR the surface ADVANCED (its fingerprint changed — a
    slot / result click navigated to the next step). A silent no-op satisfies neither."""
    same = next((a for a in after.affordances if _ident(a) == _ident(target)), None)
    if same is not None and _is_selected(same) and not _is_selected(target):
        return True
    return before.fingerprint() != after.fingerprint()


class ChooseOne:
    """The reference ``choose_one`` :class:`~zu_core.ports.InteractionPrimitive` (#125).

    With a HINT it makes ONE choice — positional ('earliest'/'last') or token-matched —
    from ANY equivalent-option group (a variant swatch/radio group or ``<select>``, a
    slot day/time grid, a result/service list), verifying the pick took. With NO hint it
    satisfies the required variant groups by delegating to
    :class:`~zu_tools.selection.FirstOptionSelectionSatisfier` — so the shopping variant
    step is unchanged and ``choose_one`` unifies both."""

    __zu_interface__ = 1  # the interaction_primitives interface major this targets
    name = "choose_one"
    kind = "choose_one"
    self_gating = True   # with NO hint it satisfies required variant groups for free
    free_priority = 20
    purpose = ("pick ONE option from an equivalent-option group — a variant swatch/radio "
               "group or <select>, a slot day/time grid, a result/service list")
    accepts_hint = True
    hint_help = ("which option to choose: a positional word ('earliest'/'first'/'last') or "
                 "a content-free token to match against the option names")

    def __init__(self, satisfier: FirstOptionSelectionSatisfier | None = None) -> None:
        self._satisfier = satisfier or FirstOptionSelectionSatisfier()

    def inspect(self, view: SurfaceView, *, hint: str | None = None) -> PrimitivePlan:
        if hint is None:
            # Applicable when a required single choice is still unmade: an UNSET native
            # <select> (a placeholder value), or a strong group with no selected option.
            unset_select = any(
                not a.value for a in _comboboxes(view)
            )
            unset_group = any(
                not any(_is_selected(a) for a in g) for g in _strong_groups(view)
            )
            handles = tuple(
                a.handle for g in _strong_groups(view) for a in _enabled_opts(g)
            ) + tuple(a.handle for a in _comboboxes(view))
            return PrimitivePlan(
                kind=self.kind,
                applicable=unset_select or unset_group,
                handles=handles,
                hint=None,
                detail="satisfy required variant group(s)",
            )
        target = resolve(view, hint)
        if target is not None:
            return PrimitivePlan(
                kind=self.kind, applicable=True, handles=(target.handle,), hint=hint,
                detail=f"choose option matching {hint!r}",
            )
        # No option matched the hint, but a <select> may hold the option internally
        # (its options are not on the surface) — let apply try a hinted select.
        combo = _comboboxes(view)
        if combo:
            return PrimitivePlan(
                kind=self.kind, applicable=True, handles=(combo[0].handle,), hint=hint,
                detail="choose <select> option by hint",
            )
        return PrimitivePlan(kind=self.kind, applicable=False, hint=hint)

    async def apply(
        self, surface: ConnectedSurface, *, hint: str | None = None
    ) -> PrimitiveOutcome:
        if hint is None:
            results = await self._satisfier.satisfy_required(surface)
            return PrimitiveOutcome(
                kind=self.kind,
                progress="advance" if results else "no_op",
                handles=tuple(r.handle for r in results),
                detail=f"set {len(results)} required option(s)",
            )
        before = await surface.perceive()
        target = resolve(before, hint)
        if target is not None:
            after = await surface.act(SurfaceAction(handle=target.handle, kind="click"))
            took = _verify(before, after, target)
            return PrimitiveOutcome(
                kind=self.kind,
                progress="advance" if took else "no_op",
                handles=(target.handle,),
                detail=f"chose {target.label!r}" if took else "click had no effect",
            )
        combo = _comboboxes(before)
        if combo:
            box = combo[0]
            after = await surface.act(SurfaceAction(handle=box.handle, kind="select", text=hint))
            # A select changes the control's VALUE (its label — the field name — is
            # stable), so re-find by position: the first combobox on the re-perceived
            # surface is the same control. 'took' iff a real, matching option landed.
            after_combos = _comboboxes(after)
            chosen = after_combos[0] if after_combos else None
            took = chosen is not None and bool(chosen.value) and chosen.value != box.value
            return PrimitiveOutcome(
                kind=self.kind,
                progress="advance" if took else "no_op",
                handles=(box.handle,),
                detail="selected <select> option" if took else "no matching option",
            )
        return PrimitiveOutcome(kind=self.kind, progress="no_op", detail="no group to choose from")
