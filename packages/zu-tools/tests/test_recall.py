"""recall — querying the run's own earlier-retrieved content.

A tool receives ``ctx.events`` (the whole run log); recall scans the
``data.source.fetched`` events for a keyword and returns the matching excerpts —
so content that scrolled out of the live context is retrievable, not lost.
"""

from __future__ import annotations

import types

from zu_tools.recall import Recall


def _ev(source: str, **content) -> object:
    return types.SimpleNamespace(type="data.source.fetched", source=source, payload=content)


def _ctx(events) -> object:
    return types.SimpleNamespace(events=events)


async def test_recall_finds_matching_excerpts_from_earlier_fetches() -> None:
    ctx = _ctx([
        _ev("http_fetch", html="<p>nothing here</p>"),
        _ev("browser", content="Choose a time slot Mon Jun 22 Morning 10:15 11:30 Afternoon"),
    ])
    out = await Recall()(ctx, query="10:15")
    assert out["matches"] >= 1
    assert "10:15" in out["content"] and "browser" in out["content"]


async def test_recall_searches_all_content_keys() -> None:
    ctx = _ctx([_ev("render_dom", text="booking_url https://vetstoria.example/book here")])
    out = await Recall()(ctx, query="booking_url")
    assert "vetstoria.example/book" in out["content"]


async def test_recall_reports_no_match_clearly() -> None:
    out = await Recall()(_ctx([_ev("http_fetch", html="abc")]), query="zzz-not-present")
    assert out["matches"] == 0 and "nothing" in out["content"].lower()


async def test_recall_ignores_non_fetched_events() -> None:
    other = types.SimpleNamespace(type="harness.turn.completed", source="model",
                                  payload={"text": "10:15 the model said this"})
    out = await Recall()(_ctx([other]), query="10:15")
    assert out["matches"] == 0   # only retrieved content is recallable, not model output


async def test_recall_caps_returned_text() -> None:
    big = "needle " + "x" * 100_000 + " needle " + "y" * 100_000
    out = await Recall()(_ctx([_ev("http_fetch", content=big)]), query="needle", max_chars=1000)
    assert len(out["content"]) < 3000   # bounded regardless of how much matched


async def test_recall_is_pure_cpu_tier_1() -> None:
    r = Recall()
    assert r.tier == 1 and r.capabilities == frozenset() and r.egress == frozenset()


async def test_recall_excerpt_aligned_for_length_changing_unicode() -> None:
    # Issue #65 F53: 'İ'.lower() is TWO code points, so ``text.lower().find(q)``
    # returns an index into the LOWERED string that is offset from the original by
    # one per such char. The old code sliced the ORIGINAL by that lowered index and
    # a window that mispositioned the match. With ENOUGH length-changing chars ahead
    # of the needle the accumulated delta (300) exceeds the ±240 window, so the old
    # slice misses the needle ENTIRELY — proving the bug, not just a cosmetic shift.
    # The fix does index math on the original string, so the excerpt is aligned and
    # the needle is present.
    text = "İ" * 300 + ("x" * 50) + "NEEDLE" + ("y" * 50)
    out = await Recall()(_ctx([_ev("http_fetch", content=text)]), query="needle")
    assert out["matches"] == 1
    assert "NEEDLE" in out["content"]  # old math sliced past it and returned ''


async def test_recall_excerpt_is_a_slice_of_the_original() -> None:
    # The returned excerpt must be a real slice of the ORIGINAL text (correct
    # case + surrounding context), not of a length-changed ``lower()`` view.
    text = "İ" * 5 + "prefix-context " + "FINDME" + " trailing-context"
    out = await Recall()(_ctx([_ev("http_fetch", content=text)]), query="findme")
    assert out["matches"] == 1
    assert "FINDME" in out["content"]  # original case preserved
    assert "prefix-context" in out["content"] and "trailing-context" in out["content"]
