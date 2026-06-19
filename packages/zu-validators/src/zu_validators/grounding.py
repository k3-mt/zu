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


# Separators that join a number to more digits to form a single larger value or
# a compound numeric token: decimal/thousands (``.`` ``,``) AND the connectors in
# dates, versions, times, ranges, SKUs and phone numbers (``-`` ``/`` ``:``). A
# match flanked by one of these with a digit on its *outer* side is a fragment of
# a longer token, not a standalone value — so "12" is not grounded by "12-2024",
# nor "30" by "12:30", just as "14" is not grounded by "3.14".
_NUM_SEPARATORS = frozenset(".,-/:")


def _standalone(corpus: str, lo: int, hi: int) -> bool:
    """Are the chars flanking ``corpus[lo:hi]`` token boundaries, not part of a
    longer alphanumeric token or a larger/compound number?"""
    before = corpus[lo - 1] if lo > 0 else ""
    after = corpus[hi] if hi < len(corpus) else ""
    if before.isalnum() or after.isalnum():
        return False
    # A numeric separator adjacent to a digit on its outer side means this match
    # is a slice of a larger number or compound token (e.g. "14" inside "3.14",
    # "12" inside "12-2024", "30" inside "12:30").
    if before in _NUM_SEPARATORS and corpus[lo - 2 : lo - 1].isdigit():
        return False
    if after in _NUM_SEPARATORS and corpus[hi + 1 : hi + 2].isdigit():
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
    # Fall back to the current observation ONLY when the event log has no fetched
    # content yet (the loop wires the full log in build step 4). If fetched events
    # exist, we must not also fold in the raw observation: an observation that is
    # not itself retrieved page content (e.g. a model-produced turn that happens
    # to carry a ``text`` key) would reopen the self-grounding hole the event-type
    # filter above exists to close.
    if not chunks:
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
        # The result value is usually a JSON object, but the schema may permit a
        # non-object root (a list or scalar). Don't assume ``.items()`` — that
        # would raise AttributeError and silently break the validator ladder.
        value = result.value
        fields = value.items() if isinstance(value, dict) else [("value", value)]
        for field, field_value in fields:
            for leaf in _leaf_strings(field_value):
                if not _grounded(_normalize(leaf), corpus):
                    return Verdict(
                        severity=Severity.RETRY,
                        detector=self.name,
                        detail=f"value for {field!r} not found in retrieved content",
                    )
        return None
