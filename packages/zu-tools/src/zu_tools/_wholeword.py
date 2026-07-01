"""_wholeword — shared accessible-name matching for the content-free web resolvers.

The consent (#94) and checkout (#117) resolvers both choose a control by its
accessible NAME, and both hit the same trap: as a bare substring, an accept/advance
word matches INSIDE a longer word — 'ok' in 'Bespoke', 'yes' in 'Eyes', 'allow' in
'Swallow'. So a *positive* match (the control we WILL click) is made on WHOLE WORDS.
A *negative*/exclusion match (a control we must NOT click, e.g. a commit button) uses
substring containment on purpose: over-excluding only ever makes us skip a control
(safe — the host takes over), never click the wrong one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

_WORD_RE = re.compile(r"[a-z0-9]+")


def word_tokens(text: str) -> list[str]:
    """Lowercase alphanumeric word tokens — the unit whole-word matching compares."""
    return _WORD_RE.findall(text.lower())


def contains_phrase(tokens: list[str], phrase: list[str]) -> bool:
    """Does ``phrase`` (a token list) occur as a contiguous run in ``tokens``? The
    whole-word test: 'ok' matches ['ok'] but not ['bespoke']."""
    n, m = len(tokens), len(phrase)
    if m == 0 or m > n:
        return False
    return any(tokens[i : i + m] == phrase for i in range(n - m + 1))


def matches_whole_word(label: str, phrases: Iterable[str]) -> bool:
    """True iff any ``phrase`` (a word or space-separated words) occurs in ``label``
    as a whole-word run — the safe test for a control we intend to CLICK."""
    tokens = word_tokens(label)
    return any(contains_phrase(tokens, phrase.split()) for phrase in phrases)


def contains_any(label: str, needles: Iterable[str]) -> bool:
    """True iff ``label`` contains any ``needle`` as a lowercase SUBSTRING — the
    deliberately-broad test for an EXCLUSION (over-matching is the safe direction:
    it makes us skip a control, never click a wrong one)."""
    low = label.lower()
    return any(n in low for n in needles)
