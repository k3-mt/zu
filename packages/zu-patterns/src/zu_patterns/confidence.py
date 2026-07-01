"""The ONE source of the recognizers' confidence thresholds (issue #65 F19).

Every built-in recognizer scores its match on the SAME small ladder of confidence
tiers — "a dominant, unambiguous match", "a structural match with corroborating
context", "a bare structural group with nothing confirming it", and so on. Those
tiers were previously copy-pasted as bare float literals (``0.85``/``0.8``/``0.65``
…) across all ten recognizers, with no shared source: a change to the ladder meant
editing ten files, and a stray ``0.8`` read as a magic number with no meaning.

This module names each tier ONCE. A recognizer imports the tier it means
(``from .confidence import STRONG``) so the number is self-documenting and the
ladder is edited in exactly one place. These are GENERIC calibration constants —
the strength of a structural match — not site constants: they say nothing about
any particular site, only how confident a given SHAPE of evidence makes a match.

The tiers are ordered high→low; ``MIN_CONFIDENCE`` is the recognizer's default
accept threshold (a hit below it is a low-confidence fall-through — no hint).
"""

from __future__ import annotations

# --- the confidence ladder (high → low), each tier named for the evidence it means ---

# A dominant, unambiguous match: the surface is essentially nothing BUT the
# archetype (a tiny banner that is only accept/reject, etc.).
DOMINANT = 0.9

# A strong match: the archetype's structure PLUS corroborating context/state
# (a checkout cluster with line-item context; an expanded combobox with options).
STRONG = 0.85

# A solid match: the archetype's structure inside its expected container
# (a sortable header within a table; a modal with dialog-ish context).
GOOD = 0.8

# A structural group with the right roles but nothing yet confirming it
# (a selectable role-group with no visible selected state).
MODERATE = 0.75

# The minimal required elements present, but weakly corroborated (a bare login
# form: a username + password field, before the submit-button/password-state bonus).
BASE = 0.7

# A weak-but-actionable match: context alone, or a bare state-only group
# (a subscribe CONTEXT with no worded button; a styled-swatch group with no role).
WEAK = 0.65

# A role-substitute fallback: the archetype recognised via a labelled proxy rather
# than its dedicated role (a plain textbox merely labelled "search", not a searchbox).
TENTATIVE = 0.62

# The minimal fallback floor — the archetype's least-corroborated recognisable form.
LOW = 0.6

# --- additive bonuses that lift a base tier when extra evidence is present ---

# A committing/submit control is present (lifts a form off its BASE tier).
SUBMIT_BONUS = 0.15
# A locale-independent structural STATE confirms a field (e.g. a password state).
STATE_BONUS = 0.1
# A second, weaker corroboration agrees (context AND label both consent-ish).
CORROBORATION_BONUS = 0.05

# The recognizer's default accept threshold: a hit at or above this is surfaced as
# the confident prior; below it is a low-confidence fall-through (NO hint).
MIN_CONFIDENCE = LOW
