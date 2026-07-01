"""Shared, purely-structural matching helpers for the built-in patterns.

These are deterministic predicates over a core ``SurfaceView``'s affordances —
roles, normalized labels, states. No model, no site constants: a pattern derives
its ``label_hint`` from the surface it matched, never from a hardcoded magic
string. The token lists below (``user``/``password``/``search``/``accept`` …) are
GENERIC, language-of-the-archetype vocabulary — the same way an accessibility
checker knows ``button`` means "actionable" — not site-specific keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from zu_core.surface import SurfaceAffordance, SurfaceView


def norm(s: str) -> str:
    """Lowercase, collapse whitespace — the canonical form for label matching."""
    return " ".join(s.lower().split())


# The alphanumeric "words" of a label — the unit a short/symbol token matches
# against by WHOLE-TOKEN equality (issue #57). ``re.UNICODE`` word chars so a
# non-ASCII label still tokenizes; the split is on non-word runs so a symbol like
# "×" or ">" is NEVER a substring of a word.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _is_short_or_symbol(token: str) -> bool:
    """A token that must match on a WORD BOUNDARY, not as a raw substring: a bare
    symbol (no alphanumeric char at all — "×", ">", "<") or a very short word
    (<=2 alphanumeric chars — "x", "go", "ok"). These over-match unrelated labels
    under substring semantics ("x" ∈ "Relax"/"Export", "go" ∈ "Google", "ok" ∈
    "Lookout"), so they are matched as whole tokens. Longer/multi-word tokens keep
    substring semantics (so "log in" still matches "Please log in below")."""
    if not any(ch.isalnum() for ch in token):
        return True  # a pure symbol
    return len(token) <= 2


def token_matches(label_norm: str, token: str) -> bool:
    """True iff ``token`` matches the already-normalized ``label_norm``.

    Short/symbol tokens (issue #57) match on a WORD BOUNDARY — the token must be a
    whole word of the label (for an alphanumeric token) or literally present with
    a non-word neighbourhood (for a bare symbol like "×"/">") — never a substring
    inside a longer word. Every other token keeps substring containment so
    multi-word/longer vocabulary ("log in", "add to cart") still matches inside a
    richer label. ``token`` is normalized here so callers may pass raw vocabulary."""
    tok = norm(token)
    if not tok:
        return False
    if _is_short_or_symbol(tok):
        if any(ch.isalnum() for ch in tok):
            # An alphanumeric short token: must be a WHOLE word of the label.
            return tok in _WORD_RE.findall(label_norm)
        # A pure symbol ("×", ">", "<"): present, but not glued inside a word — a
        # word boundary (\b won't help across symbols) means a non-alnum neighbour.
        return bool(re.search(r"(?<!\w)" + re.escape(tok) + r"(?!\w)", label_norm))
    return tok in label_norm


def label_matches_tokens(label: str, tokens: Iterable[str]) -> bool:
    """True iff the (raw) ``label`` matches ANY token, with #57 word-boundary
    handling for short/symbol tokens. The shared any-of matcher every consumer of
    the archetype vocabularies goes through — patterns AND the rail layer (#46)."""
    lbl = norm(label)
    return any(token_matches(lbl, t) for t in tokens)


def label_has(aff: SurfaceAffordance, tokens: Iterable[str]) -> bool:
    """True iff the affordance's normalized label matches any token.

    Short/symbol tokens ("x", "go", "ok", "×", ">") match on a WORD BOUNDARY, not
    as a raw substring, so a bare "x" close token does not match "Relax"/"Export"
    and "go" does not match "Google" (issue #57). Longer/multi-word tokens keep
    substring semantics. Generic across every consumer of the vocabularies."""
    return label_matches_tokens(aff.label, tokens)


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
# Shipping/contact slot vocabulary — the address-form language of the archetype.
SHIPPING_TOKENS = (
    "postcode",
    "postal code",
    "zip",
    "city",
    "town",
    "address",
    "address line",
    "street",
    "full name",
    "first name",
    "last name",
    "phone",
    "telephone",
    "mobile",
    "county",
    "state",
)
# Roles that name a SELECTABLE option — the structural vocabulary of a
# variant/swatch/radio-like picker (#39). A group of these, each carrying a
# selected/checked/pressed-style state slot, IS a picker regardless of the product
# vocabulary in their labels — recognized by STRUCTURE, never by name.
SELECTABLE_ROLES = ("radio", "option", "tab", "swatch", "menuitemradio", "listitem")
# The state tokens that read as "this option is the chosen one" — content-free
# selection markers (aria-selected/checked/pressed and their plain synonyms).
SELECTED_STATES = ("selected", "checked", "pressed", "active", "aria-selected")
# One-time-code vocabulary — an OTP/verification field is a committing-form tell.
OTP_TOKENS = (
    "otp",
    "one-time",
    "one time",
    "verification code",
    "security code",
    "passcode",
    "2fa",
    "authentication code",
)
# Subscribe/join vocabulary — the newsletter-signup language of the archetype.
SUBSCRIBE_TOKENS = (
    "subscribe",
    "sign up",
    "sign-up",
    "signup",
    "join",
    "newsletter",
    "subscribe now",
    "get updates",
    "stay updated",
)
