"""recall — query THIS run's own earlier-retrieved content (tier 1).

A long agentic run fetches more than fits in the model's context window. Rather
than *losing* old observations to compaction, the run keeps them: every fetch is
on the event log in full (``data.source.fetched``), and a tool receives the whole
log as ``ctx.events``. ``recall`` makes that history queryable — give it a keyword
and it returns the matching excerpts from pages/responses you already retrieved
(even ones that have since scrolled out of the live context). Working memory stays
lean; long-term memory stays reachable.

Pure CPU over the run's own log — no network, no capability. It complements the
context bound: an elided observation isn't gone, it's a ``recall`` away.
"""

from __future__ import annotations

from typing import Any

_CONTENT_KEYS = ("content", "text", "html")
_WINDOW = 240  # chars of context to return on each side of a match


def _ci_find(text: str, query: str, start: int) -> int:
    """Case-insensitive search that returns an index into the ORIGINAL ``text``.

    ``str.lower()`` is NOT length-preserving for some Unicode (``'İ'`` → ``'i̇'``
    is two code points; ``'ﬀ'`` → ``'ff'``), so ``text.lower().find(q)`` yields an
    index into the *lowered* string that no longer aligns with ``text`` — slicing
    the original by it garbles the excerpt (issue #65 F53). To keep index math on
    the original string, we lower each single character only for the *comparison*
    and advance one ORIGINAL character at a time, comparing the lowered query
    against the running-lowered window. The returned index is an offset into
    ``text`` (a real character boundary), so ``text[i:...]`` stays aligned.
    """
    return _ci_find_span(text, query, start)[0]


def _ci_find_span(text: str, query: str, start: int) -> tuple[int, int]:
    """Like :func:`_ci_find` but returns ``(start_index, end_index)`` — both
    offsets into the ORIGINAL ``text`` — so the caller slices the original by
    real character boundaries. ``(-1, -1)`` when there is no match. ``end`` is the
    original index just past the last matched character, which may span more (or
    fewer) original chars than ``len(query)`` when a char lowers to several units.
    """
    q = query.lower()
    if not q:
        return -1, -1
    n = len(text)
    for i in range(max(0, start), n):
        # Grow a lowered candidate from position i until it covers the query
        # prefix or diverges — a char may lower to >1 unit, so compare by prefix.
        cand = ""
        j = i
        while j < n and len(cand) < len(q):
            cand += text[j].lower()
            j += 1
        if cand[: len(q)] == q:
            return i, j
    return -1, -1


class Recall:
    name = "recall"
    tier = 1
    schema = {
        "name": "recall",
        "description": (
            "Search content you ALREADY retrieved earlier this run (pages/responses, "
            "including ones that scrolled out of context) for a keyword, and get the "
            "matching excerpts back. Use it to pull back a value/url you saw before."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "a keyword/substring to find"},
                "max_chars": {"type": "integer", "description": "cap on returned text (optional)"},
            },
            "required": ["query"],
        },
    }
    prompt_fragment = (
        "recall(query): search content you already retrieved earlier this run for a "
        "keyword and get the matching excerpts — to pull back something out of context."
    )
    capabilities: frozenset[str] = frozenset()   # reads the run's own log; no off-box reach
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx: Any, query: str, max_chars: int = 4000) -> dict:
        q = (query or "").lower()
        if not q:
            return {"query": query, "matches": 0, "content": "(empty query)"}
        excerpts: list[str] = []
        total = 0
        for ev in getattr(ctx, "events", []) or []:
            if getattr(ev, "type", "") != "data.source.fetched":
                continue
            payload = getattr(ev, "payload", {}) or {}
            source = getattr(ev, "source", "") or "?"
            for key in _CONTENT_KEYS:
                text = payload.get(key)
                if not isinstance(text, str):
                    continue
                # Case-insensitive search whose indices are into the ORIGINAL
                # ``text`` (issue #65 F53) — NOT into a ``text.lower()`` that a
                # length-changing char (e.g. 'İ'→'i̇') would misalign — so every
                # slice below stays aligned to real character boundaries.
                start = 0
                while total < max_chars:
                    i, match_end = _ci_find_span(text, query, start)
                    if i < 0:
                        break
                    a, b = max(0, i - _WINDOW), min(len(text), match_end + _WINDOW)
                    excerpt = text[a:b].strip()
                    excerpts.append(f"[{source}] …{excerpt}…")
                    total += len(excerpt)
                    start = b
                if total >= max_chars:
                    break
            if total >= max_chars:
                break
        if not excerpts:
            return {"query": query, "matches": 0,
                    "content": f"(nothing you retrieved earlier matched {query!r})"}
        return {"query": query, "matches": len(excerpts), "content": "\n…\n".join(excerpts)}
