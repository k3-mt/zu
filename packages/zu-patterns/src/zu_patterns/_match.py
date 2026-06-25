"""Shared, purely-structural matching helpers for the built-in patterns.

These are deterministic predicates over a core ``SurfaceView``'s affordances —
roles, normalized labels, states. No model, no site constants: a pattern derives
its ``label_hint`` from the surface it matched, never from a hardcoded magic
string. The token lists below (``user``/``password``/``search``/``accept`` …) are
GENERIC, language-of-the-archetype vocabulary — the same way an accessibility
checker knows ``button`` means "actionable" — not site-specific keys.
"""

from __future__ import annotations

from collections.abc import Iterable

from zu_core.surface import SurfaceAffordance, SurfaceView


def norm(s: str) -> str:
    """Lowercase, collapse whitespace — the canonical form for label matching."""
    return " ".join(s.lower().split())


def label_has(aff: SurfaceAffordance, tokens: Iterable[str]) -> bool:
    """True iff the affordance's normalized label contains any token."""
    lbl = norm(aff.label)
    return any(t in lbl for t in tokens)


def has_state(aff: SurfaceAffordance, *states: str) -> bool:
    sset = {norm(s) for s in aff.states}
    return any(norm(s) in sset for s in states)


def of_role(surface: SurfaceView, *roles: str) -> list[SurfaceAffordance]:
    rset = {r.lower() for r in roles}
    return [a for a in surface.affordances if a.role.lower() in rset]


def first(
    surface: SurfaceView,
    *,
    roles: Iterable[str] = (),
    tokens: Iterable[str] = (),
    states: Iterable[str] = (),
) -> SurfaceAffordance | None:
    """The first affordance matching ALL of the supplied predicates (any omitted
    predicate is satisfied vacuously)."""
    rset = {r.lower() for r in roles}
    tlist = list(tokens)
    slist = list(states)
    for a in surface.affordances:
        if rset and a.role.lower() not in rset:
            continue
        if tlist and not label_has(a, tlist):
            continue
        if slist and not has_state(a, *slist):
            continue
        return a
    return None


def context_has(surface: SurfaceView, tokens: Iterable[str]) -> bool:
    blob = norm(" ".join(surface.context))
    return any(t in blob for t in tokens)


# Generic archetype vocabularies (NOT site constants).
USER_TOKENS = ("user", "email", "e-mail", "login", "username", "account name")
PASSWORD_TOKENS = ("password", "passcode", "pass word")
SUBMIT_TOKENS = ("sign in", "log in", "login", "submit", "continue", "next", "go")
SEARCH_TOKENS = ("search", "find", "query", "look up")
ACCEPT_TOKENS = ("accept", "agree", "allow", "got it", "ok", "i accept", "consent")
REJECT_TOKENS = ("reject", "decline", "deny", "refuse", "no thanks")
CLOSE_TOKENS = ("close", "dismiss", "×", "x")
CONFIRM_TOKENS = ("confirm", "yes", "ok", "proceed", "continue")
NEXT_TOKENS = ("next", "next page", ">", "more", "load more")
PREV_TOKENS = ("prev", "previous", "back", "<")
CART_TOKENS = ("add to cart", "add to bag", "add to basket")
CHECKOUT_TOKENS = ("checkout", "check out")
PLACE_ORDER_TOKENS = ("place order", "buy now", "pay", "complete purchase", "confirm order")
