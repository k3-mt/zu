"""outcome inference — score a recognised control by GOAL PROGRESS, content-free (#69).

zu already says WHAT a control is (the recognizers) and HOW RISKY it is
(:class:`reversibility.Commitment` — REVERSIBLE vs COMMITTING). What it could not
yet say is WHETHER pursuing a control ADVANCES THE CALLER'S GOAL. Without it, an
agent (and zu's own #41 diagnosis) falls back on naming distractions one by one
("this is a newsletter, avoid it") — which only catches the distractions someone
already named.

This is the missing bridge: a control is on-path only when its DECLARED OUTCOME
(:attr:`RecognitionResult.outcome` — the pattern's own generic outcome vocabulary)
overlaps the goal. Avoid the footer newsletter not because it is *named*
"newsletter" but because its outcome — "subscribed" — is not "basket". That
generalises to the spin-to-win wheel, the survey modal, the currency selector and
every other off-path widget at once, including UNNAMED ones.

Content-free by construction: derived ONLY from the pattern's declared outcome
tokens and the goal's tokens — never page text fed to a model as instructions.
Reuses :class:`reversibility.Signal` (a signed weight) so the same evidence shape
that declares RISK also declares RELEVANCE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from zu_core.ports import RecognitionResult

from .reversibility import Signal

# Tokens that carry no goal/outcome meaning — dropped so a single shared filler
# word ("now", "go") cannot make an off-path control read as on-path.
_STOP = frozenset(
    {
        "a", "an", "the", "to", "then", "and", "or", "of", "for", "with", "into",
        "on", "in", "it", "my", "your", "go", "now", "get", "up", "out", "stay",
        "through", "this", "that", "is", "be", "by", "at", "as",
    }
)
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> frozenset[str]:
    """Normalise free text to a content-free word set (lowercase, drop fillers and
    one-character tokens). A phrase like ``"place order"`` becomes ``{place, order}``
    so it overlaps a goal that says ``"place-order button"``."""
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1)


@dataclass(frozen=True)
class GoalContext:
    """The caller's spoken intent, normalised to content-free tokens.

    ``goal`` is the natural-language objective ("buy a dog collar … add to basket,
    then checkout to place-order"). Only its tokens are ever compared — the text is
    never handed to a model as instructions."""

    goal: str

    def tokens(self) -> frozenset[str]:
        return _tokens(self.goal)


def goal_progress(goal: GoalContext, recognition: RecognitionResult) -> Signal:
    """Does acting on this recognised control plausibly ADVANCE the goal?

    Returns a :class:`reversibility.Signal` whose ``weight`` is the relevance:

    * ``+1.0`` — **on-path**: the control's declared outcome overlaps the goal
      (e.g. a cart/checkout outcome under a "buy" goal).
    * ``-1.0`` — **off-path** side-quest: the control declares a definite outcome
      the goal does not want (e.g. a "subscribed" newsletter outcome under "buy").
    * ``0.0`` — **unknown**: the control declares no outcome, so relevance can't be
      judged (never treated as off-path).

    Pure and content-free: the only inputs are the declared outcome tokens and the
    goal tokens."""
    out: frozenset[str] = frozenset().union(*(_tokens(t) for t in recognition.outcome)) \
        if recognition.outcome else frozenset()
    if not out:
        return Signal(name=f"progress:unknown:{recognition.archetype}", weight=0.0)
    if goal.tokens() & out:
        return Signal(name=f"progress:on_path:{recognition.archetype}", weight=1.0)
    return Signal(name=f"progress:off_path:{recognition.archetype}", weight=-1.0)


def is_relevant_blocker(goal: GoalContext, recognition: RecognitionResult) -> bool:
    """The diagnosis-side rule the #41 content_view consumers want.

    When an action has NO effect, a required/blocking control is a genuine
    **blocker** only if resolving it is on-path for the goal
    (``goal_progress >= 0``). An OFF-PATH required widget — a footer newsletter
    "ENTER YOUR EMAIL" box carrying its own HTML ``required`` attribute under a
    "buy a collar" goal — is NOISE, not a boundary. This subsumes the #67
    newsletter fix and the consumer-side ``fill_region`` stopgap, and extends to
    distractions no one has named."""
    return goal_progress(goal, recognition).weight >= 0.0
