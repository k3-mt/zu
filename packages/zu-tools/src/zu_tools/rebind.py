"""Bounded stale-handle re-binding — recover a detached element by IDENTITY, not by handle
(the navigation-reliability layer's live retry-on-stale primitive).

A click that re-renders a reactive page detaches and renumbers its controls: the handle the
model is about to act on no longer locates. The competitors recover by RE-RESOLVING the same
logical control — agent-browser re-queries the accessibility tree and re-matches role + name +
nth occurrence to a fresh node; browser-use recovers a stale node and bounds it with a failure
budget. zu does the same, but keeps the model holding only an OPAQUE handle (never a selector,
the §11.3 confused-deputy invariant): the runtime owns the handle → {role, name} binding and
silently re-binds it.

On a stale locate this re-captures the surface (``op=axtree`` → ``reduce_surface``), refreshes
the run's shared handle_map so the model's handles stay valid, finds the SAME control by
(role, name, nth) on the fresh surface, and re-locates — up to ``Budget.stale_retries_max``
times. Every attempt is recorded (the tool folds it into its observation; the loop emits
``data.handle.rebound``). Exhaustion returns ``None`` so the caller surfaces the stale handle
and escalates into the existing gated grounding/vision ladder — never an unbounded loop, never
model-authored recovery code.

Deterministic and replayable offline: a fake session that fails the first locate then succeeds
after a re-capture drives the exact same path every run, no live browser.
"""

from __future__ import annotations

from typing import Any

from ._session import handle_map_of, put_handle_map, run_key
from .action_surface import normalize_axtree, reduce_surface


def stale_retries_max(ctx: Any) -> int:
    """The run's stale-retry cap from ``ctx.spec.budget.stale_retries_max`` — 0 (disabled) when
    a ctx carries no budget, so the primitive is inert unless a real run drives it. Defensive: a
    malformed/absent budget yields 0, never a crash."""
    budget = getattr(getattr(ctx, "spec", None), "budget", None)
    try:
        return max(0, int(getattr(budget, "stale_retries_max", 0)))
    except (TypeError, ValueError):
        return 0


def _ident(loc: dict) -> tuple[str, str]:
    """A locator's render-stable identity: role + accessible name, lowercased."""
    return (str(loc.get("role", "")).strip().lower(), str(loc.get("name", "")).strip().lower())


def _nth_of(handle_map: dict[str, dict], handle: str, ident: tuple[str, str]) -> int:
    """The occurrence index of ``handle`` among the handles sharing its identity, in document
    order — so the re-bind targets the SAME one of several same-named controls. 0 when it can't
    be determined (the common unique-label case re-binds to the only match anyway)."""
    same = [h for h, loc in handle_map.items() if _ident(loc) == ident]
    return same.index(handle) if handle in same else 0


def _nth_handle(surface: Any, ident: tuple[str, str], nth: int) -> str | None:
    """The handle of the ``nth`` affordance matching ``ident`` on a fresh surface (document
    order), or None when the control is gone — the role+name+nth re-resolution."""
    same = [a.handle for a in surface.affordances
            if (a.role.strip().lower(), a.label.strip().lower()) == ident]
    if not same:
        return None
    return same[min(nth, len(same) - 1)]


async def rebind_stale_handle(
    ctx: Any, session: Any, handle: str, locator: dict, *, retries_max: int, rebounds: list[dict]
) -> dict | None:
    """Bounded re-resolve-by-identity. Each attempt re-captures the surface, refreshes the
    shared handle_map, re-binds the SAME (role, name, nth) control to its fresh handle, and
    re-locates. Returns the successful ``locate`` response (with bounds) or None when it could
    not re-bind within ``retries_max``. Appends one record per attempt to ``rebounds``."""
    key = run_key(ctx)
    ident = _ident(locator)
    # The original occurrence index — read from the CURRENT shared map, BEFORE any re-capture
    # replaces it (so we re-bind the same one of several same-named controls).
    nth = _nth_of(handle_map_of(key), handle, ident)
    for attempt in range(1, retries_max + 1):
        resp = await session.send({"op": "axtree"})
        if not isinstance(resp, dict) or resp.get("axtree") is None:
            return None  # the session can't re-capture — nothing to re-bind against
        surface = reduce_surface(
            normalize_axtree([n for n in resp["axtree"] if isinstance(n, dict)]),
            title=str(resp.get("title", "")), url=str(resp.get("url", "")),
        )
        put_handle_map(key, surface.handle_map)  # keep the model's handles valid post-rebind
        new_handle = _nth_handle(surface, ident, nth)
        # Record the attempt — opaque handles + role only, never the accessible name (no
        # content on the log; the locator stays harness-side).
        rebounds.append({"old_handle": handle, "new_handle": new_handle,
                         "attempt": attempt, "role": ident[0]})
        if new_handle is None:
            return None  # the control is gone from the surface — give up (bounded)
        located = await session.send({"op": "locate", "locator": surface.handle_map[new_handle]})
        if isinstance(located, dict) and located.get("bounds") is not None:
            return located  # re-bound and re-located — the act can proceed
    return None  # exhausted the retry budget
