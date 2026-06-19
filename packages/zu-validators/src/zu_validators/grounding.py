"""grounding — every extracted value must appear in retrieved content.

The anti-making-things-up check: a value the agent reports that is nowhere in
the content the run actually fetched fails grounding. It reads the run's
content from the event log via RunContext, so it proves provenance, not just
plausibility.

Matching is token-boundary-aware (build step 6): a value must appear in the
retrieved content as a standalone token, not merely as a substring, so a short
value such as ``"5"`` is not spuriously grounded by ``"1985"``.
"""

from __future__ import annotations

from typing import Iterator

from zu_core.contracts import Result
from zu_core.ports import RunContext, Severity, Verdict


def _normalize(s: str) -> str:
    """Collapse whitespace and lowercase so trivial formatting differences
    between an extracted value and the page text don't cause false failures."""
    return " ".join(s.split()).lower()


def _grounded(leaf_norm: str, corpus: str) -> bool:
    """Is the normalized value present in the corpus on token boundaries?

    Plain substring containment is too lenient: a short value like ``"5"`` would
    match incidentally inside ``"1985"`` and let a fabricated number pass. We
    require the value to appear as a standalone token, not a fragment of a longer
    one, on two axes:

    - **Alphanumeric flanks** (Unicode-aware via ``str.isalnum``): ``"5"`` inside
      ``"1985"`` or ``"caf"`` inside ``"café"`` does not ground, while ``"$9.00"``
      between ``>`` and ``<`` still does — punctuation is a boundary.
    - **Number fragments across a decimal/thousands separator**: a ``.`` or ``,``
      flanked by a digit on the *outer* side means the value is part of a larger
      number, so ``"14"`` is not grounded by ``"3.14"`` nor ``"3"`` by ``"3.14"``
      — but ``"5"`` in ``"Qty: 5."`` (the dot ends a sentence) still grounds.
    """
    if not leaf_norm:
        return True
    n = len(leaf_norm)
    start = 0
    while True:
        i = corpus.find(leaf_norm, start)
        if i == -1:
            return False
        if _standalone(corpus, i, i + n):
            return True
        start = i + 1


def _standalone(corpus: str, lo: int, hi: int) -> bool:
    """Are the chars flanking ``corpus[lo:hi]`` token boundaries, not part of a
    longer alphanumeric token or a larger number?"""
    before = corpus[lo - 1] if lo > 0 else ""
    after = corpus[hi] if hi < len(corpus) else ""
    if before.isalnum() or after.isalnum():
        return False
    # A decimal/thousands separator adjacent to a digit on its outer side means
    # this match is a slice of a larger number (e.g. "14" inside "3.14").
    if before in ".," and corpus[lo - 2 : lo - 1].isdigit():
        return False
    if after in ".," and corpus[hi + 1 : hi + 2].isdigit():
        return False
    return True


def _leaf_strings(value: object) -> Iterator[str]:
    """Yield every scalar leaf of a result value as a string to ground.

    Numbers and booleans are real extracted values too — skipping non-strings
    (the previous behaviour) let a fabricated price or count pass ungrounded.
    bool is checked before int because ``isinstance(True, int)`` is True, and a
    boolean is not groundable page text.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        if text:
            yield text
    elif isinstance(value, dict):
        for v in value.values():
            yield from _leaf_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _leaf_strings(v)


def _retrieved_corpus(ctx: RunContext) -> str:
    """Concatenate everything the run fetched, from data.source.fetched events.

    Falls back to the current observation when the event log isn't populated
    yet (the loop wires the full log in build step 4).
    """
    chunks: list[str] = []
    for ev in getattr(ctx, "events", []) or []:
        # Only *retrieved* content grounds a value — i.e. data.source.fetched
        # events. Reading text-like keys from any event would let the model
        # ground its own fabrications: harness.turn.completed carries the model's
        # output text, which must never count as evidence about the page.
        if getattr(ev, "type", "") != "data.source.fetched":
            continue
        payload = getattr(ev, "payload", {}) or {}
        for key in ("html", "text", "content"):
            if isinstance(payload.get(key), str):
                chunks.append(payload[key])
    obs = getattr(ctx, "observation", None)
    if isinstance(obs, dict):
        for key in ("html", "text", "content"):
            if isinstance(obs.get(key), str):
                chunks.append(obs[key])
    return "\n".join(chunks)


class GroundingValidator:
    name = "grounding"

    def check(self, result: Result, ctx: RunContext) -> Verdict | None:
        if not result.value:
            return None
        corpus = _normalize(_retrieved_corpus(ctx))
        for field, value in result.value.items():
            for leaf in _leaf_strings(value):
                if not _grounded(_normalize(leaf), corpus):
                    return Verdict(
                        severity=Severity.RETRY,
                        detector=self.name,
                        detail=f"value for {field!r} not found in retrieved content",
                    )
        return None
