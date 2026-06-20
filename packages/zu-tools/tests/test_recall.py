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
