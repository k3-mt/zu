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

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any

# Don't make replay wait the model's full think-time between steps — that was the
# model being slow, not the page needing it. But DO leave a small settle so a
# replayed click doesn't race a page that the model (by thinking) implicitly let
# settle. So a recorded gap is capped to this on replay.
MAX_REPLAY_WAIT_MS = 3000

# Replay humanisation. A track driven at machine cadence — every step fired the
# instant the last returned — is a tell. But real inter-action pauses do NOT creep
# upward as a session goes on: they cluster around a typical value with the
# occasional much longer one (the person paused to read, got distracted). That is
# a right-skewed, heavy-tailed shape — a log-normal — and it is *stationary*: the
# same range at step 2 and step 92, not position-dependent. So each replayed step
# waits a recorded floor (the track value — see ``_replay_track``) plus an extra
# drawn from a log-normal with a stable median. Seeded, so a run is reproducible.
# The typical (median) EXTRA pause added per step, on top of the recorded floor.
REPLAY_JITTER_MEDIAN_MS = 400
# Log-space spread. Larger ⇒ heavier tail. ~0.9 puts the occasional pause a second
# or two above the median while keeping the bulk near it.
REPLAY_JITTER_SIGMA = 0.9
# A hard ceiling on the EXTRA so one pathological draw can't stall a run; the tail
# can still reach a second or two ("or longer") below it.
REPLAY_JITTER_MAX_MS = 8000


def replay_extra_delay_ms(
    rng: random.Random,
    *,
    median_ms: int = REPLAY_JITTER_MEDIAN_MS,
    sigma: float = REPLAY_JITTER_SIGMA,
    max_ms: int = REPLAY_JITTER_MAX_MS,
) -> int:
    """The EXTRA delay (ms) to add on top of a step's recorded floor — drawn from a
    log-normal so most steps wait about ``median_ms`` while a few have a long tail
    (a second or two, occasionally longer), the shape of real human pauses.

    **Stationary**: it does NOT depend on the step's position in the track, so the
    pacing does not creep upward as the run goes on — the same range throughout,
    with the tail landing on whichever steps the seeded draws happen to hit.

    Pure and deterministic in the ``rng`` state: feed a seeded ``random.Random`` and
    a run replays with the same pacing. The result is capped at ``max_ms`` so a
    freak draw can't hang a run; ``median_ms <= 0`` disables it (returns 0)."""
    if median_ms <= 0:
        return 0
    # log-normal: exp(N(ln median, sigma)). Its median is exactly median_ms; the
    # mean sits a little above (right skew), and the tail is long but bounded here.
    val = rng.lognormvariate(math.log(median_ms), sigma)
    return min(int(val), max_ms)


@dataclass
class TrackStep:
    """One tool call on the path: which tool, the exact args, the gap before it
    (ms since the previous call was issued — the model's pacing, capped on replay),
    and the ladder ``tier`` the call ran at. The tier lets the track REMEMBER its
    own escalation: a step recorded at tier 2 means the path had climbed there, so
    the navigator re-climbs (emitting the escalation) before re-issuing it.

    ``consequence`` and ``destination`` are blessed consumer annotations (ZU-RAIL-4):
    a content-free consequence class (e.g. ``"LOW"``/``"HIGH"``) and a destination
    descriptor (merchant/recipient/origin). Zu does not interpret them — the
    *classifier* and the values' meaning are the consumer's policy — but it carries
    them across capture→replay and re-stamps them into the replayed
    ``harness.tool.invoked`` ``payload["ctx"]`` so a gate or a ``ReplayArbiter`` reads
    them uniformly to gate divergence by consequence."""

    tool: str
    args: dict
    wait_ms: int = 0
    tier: int = 1
    consequence: str | None = None
    destination: str | None = None


def _step_to_dict(s: TrackStep) -> dict:
    """Serialise a step, omitting the optional annotations when absent so a track
    with no consequence/destination is byte-identical to a pre-RAIL-4 track."""
    d: dict = {"tool": s.tool, "args": s.args, "wait_ms": s.wait_ms, "tier": s.tier}
    if s.consequence is not None:
        d["consequence"] = s.consequence
    if s.destination is not None:
        d["destination"] = s.destination
    return d


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
             "steps": [_step_to_dict(s) for s in self.steps]},
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> Track:
        data = json.loads(text)
        return cls(
            task=data.get("task", ""),
            model=data.get("model"),
            steps=[TrackStep(tool=s["tool"], args=s.get("args", {}),
                             wait_ms=int(s.get("wait_ms", 0)), tier=int(s.get("tier", 1)),
                             consequence=s.get("consequence"), destination=s.get("destination"))
                   for s in data.get("steps", [])],
        )

    def content_hash(self) -> str:
        """A deterministic sha256 over the rail's ordered **semantic** steps — the
        identity a human approval is bound to (ZU-RAIL-1), so replay can verify it
        is running *that exact rail*. Hashes ``tool``/``args``/``tier`` and the
        ``consequence``/``destination`` annotations; **excludes ``wait_ms``**, which
        is cosmetic pacing (humanised per run) and must not invalidate an approved
        rail. Canonical JSON like ``zu_core.chain`` — stdlib only."""
        body = [
            {"tool": s.tool, "args": s.args, "tier": s.tier,
             "consequence": s.consequence, "destination": s.destination}
            for s in self.steps
        ]
        blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

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
        # Carry the blessed step annotations (ZU-RAIL-4) if the consumer stamped
        # them into the call's payload["ctx"] during pathfinding, so consequence/
        # destination round-trip capture→replay. Zu never sets them itself.
        raw_ctx = payload.get("ctx")
        ctx: dict = raw_ctx if isinstance(raw_ctx, dict) else {}
        steps.append(TrackStep(tool=tool, args=dict(payload.get("args", {})),
                               wait_ms=wait_ms, tier=tier,
                               consequence=ctx.get("consequence"),
                               destination=ctx.get("destination")))
    return Track(task=task, steps=steps, model=model)
