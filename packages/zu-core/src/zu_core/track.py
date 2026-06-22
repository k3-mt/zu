"""Track — a recorded, replayable path of an agent's tool calls.

The model is an expensive PATHFINDER: the first time it does a task it explores,
and every tool call it makes (with the time between calls) is already on the event
log. A :class:`Track` is the projection of that log into a deterministic path — the
ordered tool calls + their pacing — saved in the agent's directory.

A navigator then DRIVES that path with no model calls (see ``navigator`` in the
loop), reproducing exactly what the model did. The model only reappears at the
frontier: when a step hits a challenge (an error) or the track runs out. So a task
done once runs cheaply forever after, and model calls are spent only on the novel.

This module is pure data + projection (SDK-free, stdlib only); the replay engine
lives in the loop, where tool dispatch already is.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

# Don't make replay wait the model's full think-time between steps — that was the
# model being slow, not the page needing it. But DO leave a small settle so a
# replayed click doesn't race a page that the model (by thinking) implicitly let
# settle. So a recorded gap is capped to this on replay.
MAX_REPLAY_WAIT_MS = 3000

# Replay humanisation. A track driven at machine cadence — every step fired the
# instant the last returned — is a tell: real use has growing, irregular pauses.
# So, when running a track from 0% to 100%, add a RANDOM extra delay to each step
# centred on an envelope that curves UPWARD with progress: the start is
# near-instant, the tail is the most deliberate. This is the same realism move as
# the seeded pointer path (§12) — bounded and seeded, so a run is reproducible and
# tested at $0.
# The centre delay reached as progress nears 100%.
REPLAY_JITTER_MAX_MS = 1500
# The envelope curves upward with progress (ease-in): ``progress ** CURVE``. 2.0 is
# quadratic — gentle early, steep near the end.
REPLAY_JITTER_CURVE = 2.0
# Each step's realised delay varies up AND down around the centre by up to this
# fraction of it — so a step lands above or below the rising trend, not merely
# under a one-sided cap.
REPLAY_JITTER_SPREAD = 0.5


def replay_extra_delay_ms(
    progress: float,
    rng: random.Random,
    *,
    max_extra_ms: int = REPLAY_JITTER_MAX_MS,
    spread: float = REPLAY_JITTER_SPREAD,
) -> int:
    """Extra delay (ms) before a replayed step — centred on an UPWARD-CURVING
    envelope of progress, varying up AND down around it.

    The centre rises with progress as ``max_extra_ms * progress ** REPLAY_JITTER_CURVE``
    — an ease-in curve, so the tail of the track is far more deliberate than the
    start. Each step's realised delay is an INDEPENDENT draw from a triangular
    distribution centred on that value, spanning ``±spread`` of it — so a step can
    land above or below the trend (the ms genuinely vary up and down) while the
    expected value still tracks the curve.

    Pure and deterministic in ``(progress, rng state)``: feed a seeded
    ``random.Random`` and a run replays with the same pacing. ``max_extra_ms <= 0``
    disables it. A delay is never negative — you cannot un-sleep — so "down" means
    below the rising centre, not below zero."""
    if max_extra_ms <= 0:
        return 0
    p = min(1.0, max(0.0, progress))
    center = max_extra_ms * (p**REPLAY_JITTER_CURVE)
    if center <= 0:
        return 0
    half = spread * center
    # triangular(low, high, mode): symmetric about the centre → varies up & down.
    return max(0, int(rng.triangular(center - half, center + half, center)))


@dataclass
class TrackStep:
    """One tool call on the path: which tool, the exact args, the gap before it
    (ms since the previous call was issued — the model's pacing, capped on replay),
    and the ladder ``tier`` the call ran at. The tier lets the track REMEMBER its
    own escalation: a step recorded at tier 2 means the path had climbed there, so
    the navigator re-climbs (emitting the escalation) before re-issuing it."""

    tool: str
    args: dict
    wait_ms: int = 0
    tier: int = 1


@dataclass
class Track:
    """A replayable path for a task. ``task`` is the signature it was recorded for
    (the query) — a track is only replayed for a matching task, never blindly.
    ``model`` is the provider model id that originally drove (pathfound) the run, kept
    as provenance: the path is the frontier model's reasoning, frozen for cheap reuse."""

    task: str
    steps: list[TrackStep] = field(default_factory=list)
    model: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {"task": self.task,
             "model": self.model,
             "steps": [{"tool": s.tool, "args": s.args, "wait_ms": s.wait_ms, "tier": s.tier}
                       for s in self.steps]},
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> Track:
        data = json.loads(text)
        return cls(
            task=data.get("task", ""),
            model=data.get("model"),
            steps=[TrackStep(tool=s["tool"], args=s.get("args", {}),
                             wait_ms=int(s.get("wait_ms", 0)), tier=int(s.get("tier", 1)))
                   for s in data.get("steps", [])],
        )

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> Track | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return cls.from_json(fh.read())
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None

    def matches(self, task: str) -> bool:
        """A track replays only for the task it was recorded for."""
        return bool(self.task) and self.task == task


def record_track(events: list[Any], *, task: str, model: str | None = None) -> Track:
    """Project a run's event log into a Track: every ``harness.tool.invoked`` in
    order, with the inter-call gap (ms) derived from the event timestamps and the
    ladder ``tier`` active at the call. The tier is tracked by replaying the
    ``harness.task.escalated`` events interleaved with the tool calls — so a path
    that climbed to a browser tier records those steps at that tier, and the track
    remembers its escalation. ``model`` stamps which model pathfound the run. No
    extra instrumentation; the log already captured the path."""
    steps: list[TrackStep] = []
    prev_ts = None
    tier = 1
    for ev in events:
        type_ = getattr(ev, "type", "")
        payload = getattr(ev, "payload", {}) or {}
        if type_ == "harness.task.escalated":
            to_tier = payload.get("to_tier")
            if isinstance(to_tier, int):
                tier = to_tier
            continue
        if type_ != "harness.tool.invoked":
            continue
        tool = payload.get("tool")
        if not tool:
            continue
        ts = getattr(ev, "ts", None)
        wait_ms = 0
        if prev_ts is not None and ts is not None:
            wait_ms = max(0, int((ts - prev_ts).total_seconds() * 1000))
        prev_ts = ts
        steps.append(TrackStep(tool=tool, args=dict(payload.get("args", {})),
                               wait_ms=wait_ms, tier=tier))
    return Track(task=task, steps=steps, model=model)
