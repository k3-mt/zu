"""Track — recording a replayable path from the event log, and round-tripping it."""

from __future__ import annotations

import random
import types
from datetime import UTC, datetime, timedelta

from zu_core.track import (
    REPLAY_JITTER_CURVE,
    REPLAY_JITTER_MAX_MS,
    REPLAY_JITTER_SPREAD,
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


# --- replay humanisation: upward-scaling random delays (run a track 0%→100%) ---


def test_jitter_disabled_returns_zero() -> None:
    rng = random.Random(0)
    assert replay_extra_delay_ms(1.0, rng, max_extra_ms=0) == 0
    assert replay_extra_delay_ms(1.0, rng, max_extra_ms=-100) == 0


def test_jitter_zero_at_the_start_of_the_track() -> None:
    # progress 0.0 → ceiling is 0 → no delay, however the rng is seeded
    for seed in range(20):
        assert replay_extra_delay_ms(0.0, random.Random(seed), max_extra_ms=5000) == 0


def test_jitter_is_bounded_by_the_curved_envelope() -> None:
    # at progress p the delay stays within the triangular band around the centre:
    # [center*(1-spread), center*(1+spread)] where center = max * p**curve.
    for p in (0.1, 0.25, 0.5, 0.75, 1.0):
        center = 4000 * (p ** REPLAY_JITTER_CURVE)
        hi = center * (1 + REPLAY_JITTER_SPREAD)
        for seed in range(50):
            d = replay_extra_delay_ms(p, random.Random(seed), max_extra_ms=4000)
            assert 0 <= d <= hi + 1  # +1 for the int() floor


def test_jitter_centre_tracks_the_curve() -> None:
    # The triangular distribution is symmetric about the centre, so the MEAN of
    # many independent draws tracks center = max * p**curve.
    for p in (0.4, 0.7, 1.0):
        vals = [replay_extra_delay_ms(p, random.Random(s), max_extra_ms=4000)
                for s in range(800)]
        center = 4000 * (p ** REPLAY_JITTER_CURVE)
        assert abs(sum(vals) / len(vals) - center) < 0.1 * 4000  # within 10% of max


def test_jitter_varies_up_and_down_around_the_centre() -> None:
    # At a fixed progress, independent draws land BOTH above and below the centre —
    # the ms genuinely vary up and down, not one-sided from zero.
    p, center = 0.8, 4000 * (0.8 ** REPLAY_JITTER_CURVE)
    vals = [replay_extra_delay_ms(p, random.Random(s), max_extra_ms=4000)
            for s in range(200)]
    assert min(vals) < center
    assert max(vals) > center


def test_envelope_curves_upward_convexly() -> None:
    # ease-in: equal steps in progress add MORE delay later than earlier (the
    # centre rises convexly), using the mean over seeds as the centre estimate.
    def mean_at(p: float) -> float:
        return sum(replay_extra_delay_ms(p, random.Random(s), max_extra_ms=4000)
                   for s in range(800)) / 800

    early_rise = mean_at(0.4) - mean_at(0.2)
    late_rise = mean_at(0.8) - mean_at(0.6)
    assert late_rise > early_rise


def test_jitter_is_deterministic_for_a_seed() -> None:
    # Same seeded rng → same sequence of delays across the track (a run replays
    # with identical pacing).
    def sequence() -> list[int]:
        rng = random.Random("run-42")
        return [replay_extra_delay_ms(i / 9, rng, max_extra_ms=2000) for i in range(10)]

    assert sequence() == sequence()


def test_default_constants_are_sane() -> None:
    assert REPLAY_JITTER_MAX_MS > 0
    assert REPLAY_JITTER_CURVE > 1.0       # curves upward (convex), not linear
    assert 0 < REPLAY_JITTER_SPREAD <= 1   # two-sided band that stays non-negative
