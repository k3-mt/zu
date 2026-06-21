"""Track — recording a replayable path from the event log, and round-tripping it."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta

from zu_core.track import Track, TrackStep, record_track


def _ev(type_, ts, **payload):
    return types.SimpleNamespace(type=type_, ts=ts, payload=payload)


def test_record_projects_tool_calls_with_timing() -> None:
    t0 = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
    events = [
        _ev("harness.task.started", t0),
        _ev("harness.tool.invoked", t0, tool="web_search", args={"query": "vets"}),
        _ev("harness.tool.returned", t0 + timedelta(seconds=1), tool="web_search"),
        _ev("harness.tool.invoked", t0 + timedelta(seconds=3), tool="browser",
            args={"op": "open", "url": "https://x/"}),
        _ev("data.source.fetched", t0 + timedelta(seconds=4)),
        _ev("harness.tool.invoked", t0 + timedelta(seconds=5, milliseconds=500),
            tool="browser", args={"op": "act", "actions": [{"click": "Next"}]}),
    ]
    track = record_track(events, task="find slots")
    assert track.task == "find slots"
    assert [s.tool for s in track.steps] == ["web_search", "browser", "browser"]
    assert track.steps[0].wait_ms == 0                      # first call: no prior
    assert track.steps[1].wait_ms == 3000                   # 3s after the first invoke
    assert track.steps[2].wait_ms == 2500                   # 2.5s after the second
    assert track.steps[1].args == {"op": "open", "url": "https://x/"}


def test_record_ignores_non_tool_events() -> None:
    t0 = datetime(2026, 6, 20, tzinfo=UTC)
    track = record_track([_ev("harness.turn.completed", t0, text="thinking")], task="q")
    assert track.steps == []


def test_record_remembers_tier_escalation_and_model() -> None:
    t0 = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)
    events = [
        _ev("harness.tool.invoked", t0, tool="web_search", args={"q": "x"}),
        _ev("harness.tool.invoked", t0, tool="http_fetch", args={"url": "u"}),
        _ev("harness.task.escalated", t0, from_tier=1, to_tier=2, reason="embedded-widget"),
        _ev("harness.tool.invoked", t0, tool="browser", args={"op": "open"}),
        _ev("harness.tool.invoked", t0, tool="browser", args={"op": "act"}),
    ]
    track = record_track(events, task="find slots", model="anthropic/claude-sonnet-4.5")
    assert track.model == "anthropic/claude-sonnet-4.5"
    # the tier the path had climbed to is remembered per step
    assert [(s.tool, s.tier) for s in track.steps] == [
        ("web_search", 1), ("http_fetch", 1), ("browser", 2), ("browser", 2),
    ]


def test_round_trip_json() -> None:
    track = Track(task="q", model="m/v1", steps=[
        TrackStep("web_search", {"query": "x"}, 0, tier=1),
        TrackStep("browser", {"op": "open", "url": "https://x/"}, 1200, tier=2),
    ])
    back = Track.from_json(track.to_json())
    assert back.task == "q" and back.model == "m/v1"
    assert [(s.tool, s.args, s.wait_ms, s.tier) for s in back.steps] == [
        ("web_search", {"query": "x"}, 0, 1),
        ("browser", {"op": "open", "url": "https://x/"}, 1200, 2),
    ]


def test_from_json_back_compatible_with_tierless_track() -> None:
    # An older track has no model/tier — load it without error, defaulting to tier 1.
    old = '{"task": "q", "steps": [{"tool": "browser", "args": {"op": "open"}, "wait_ms": 5}]}'
    track = Track.from_json(old)
    assert track.model is None
    assert track.steps[0].tier == 1 and track.steps[0].wait_ms == 5


def test_save_load_roundtrip(tmp_path) -> None:
    p = str(tmp_path / "track.json")
    Track(task="q", steps=[TrackStep("t", {"a": 1}, 0)]).save(p)
    loaded = Track.load(p)
    assert loaded is not None and loaded.task == "q" and loaded.steps[0].tool == "t"


def test_load_missing_or_bad_is_none(tmp_path) -> None:
    assert Track.load(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert Track.load(str(bad)) is None


def test_matches_only_the_recorded_task() -> None:
    track = Track(task="find slots", steps=[])
    assert track.matches("find slots")
    assert not track.matches("something else")
    assert not Track(task="", steps=[]).matches("")   # empty task never matches
