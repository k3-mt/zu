"""Track — recording a replayable path from the event log, and round-tripping it."""

from __future__ import annotations

import random
import types
from datetime import UTC, datetime, timedelta

from zu_core.track import (
    REPLAY_JITTER_MAX_MS,
    REPLAY_JITTER_MEDIAN_MS,
    REPLAY_JITTER_SIGMA,
    Track,
    TrackStep,
    record_track,
    replay_extra_delay_ms,
)


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


def _samples(n: int, **kw) -> list[int]:
    return [replay_extra_delay_ms(random.Random(s), **kw) for s in range(n)]


def _median(xs: list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# --- replay humanisation: a stationary, heavy-tailed extra (human-like pauses) ---


def test_jitter_disabled_returns_zero() -> None:
    rng = random.Random(0)
    assert replay_extra_delay_ms(rng, median_ms=0) == 0
    assert replay_extra_delay_ms(rng, median_ms=-100) == 0


def test_typical_delay_sits_around_the_median() -> None:
    # The log-normal's median is exactly median_ms, so the sample median tracks it.
    vals = _samples(1500, median_ms=400, sigma=0.9)
    assert abs(_median(vals) - 400) < 80  # within ~20% of the configured median


def test_distribution_is_right_skewed_with_a_long_tail() -> None:
    # Heavy right tail: mean exceeds median, and a few draws reach a second or two
    # — while the bulk stays near the median.
    vals = _samples(2000, median_ms=400, sigma=0.9)
    mean = sum(vals) / len(vals)
    assert mean > _median(vals)                 # right-skewed
    assert max(vals) > 1500                      # the tail reaches a second-plus
    # ...but it is genuinely a tail, not the norm: most steps are modest.
    near = [v for v in vals if v <= 800]
    assert len(near) / len(vals) > 0.6


def test_delay_does_not_creep_upward_over_a_run() -> None:
    # Stationary: draw a long sequence from ONE seeded rng (as a real run does) and
    # the second half is not systematically longer than the first — no upward drift.
    rng = random.Random("run-7")
    seq = [replay_extra_delay_ms(rng, median_ms=400, sigma=0.9) for _ in range(400)]
    first_half_med = _median(seq[:200])
    second_half_med = _median(seq[200:])
    assert abs(first_half_med - second_half_med) < 120  # comparable, no ramp


def test_tail_is_capped() -> None:
    # A huge sigma would otherwise produce absurd outliers; max_ms bounds them.
    vals = _samples(2000, median_ms=400, sigma=3.0, max_ms=8000)
    assert max(vals) <= 8000


def test_jitter_is_deterministic_for_a_seed() -> None:
    # Same seeded rng → same sequence of delays (a run replays with identical pacing).
    def sequence() -> list[int]:
        rng = random.Random("run-42")
        return [replay_extra_delay_ms(rng, median_ms=400) for _ in range(10)]

    assert sequence() == sequence()


def test_default_constants_are_sane() -> None:
    assert REPLAY_JITTER_MEDIAN_MS > 0
    assert REPLAY_JITTER_SIGMA > 0          # a real spread → a tail exists
    assert REPLAY_JITTER_MAX_MS > REPLAY_JITTER_MEDIAN_MS  # cap sits above the median
