"""The construction spine — chain the OFFLINE stages of the sequence into one run.

``zu build`` composes what the earlier increments shipped: replay the captured bundle
offline (stage 3), project the resilient track from that clean run (stage 4), and score
it against perturbed fixtures (stage 5) — gating the track on the resilience score. The
output is a production-ready, hardened ``track.json`` next to the agent, produced at $0:
no model, no network.

The two LIVE stages and promotion are deliberately NOT in this spine — they need keys,
network, or a registry push, and are left behind explicit seams so the cheap, testable
core stands on its own:

* **Stage 2 (capture)** is the one live step; ``zu build`` requires its output
  (``fixtures/capture.json``) and points at ``zu capture`` when it is missing.
* **Stage 6 (canary)** — one live validation run before promotion — is the live lane,
  guarded by ``_canary`` raising ``NotImplementedError`` so ``--with-canary`` fails
  loudly rather than pretending. It is the next increment.
* **Stage 7 (promote)** — ``zu pack`` / ``zu deploy`` — is left to its existing commands;
  ``zu build`` prints them as the next step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harden import HardenReport, harden
from .offline import Bundle, replay_offline


@dataclass
class StageResult:
    """One stage of the spine: its outcome and a one-line detail for the summary."""

    name: str
    status: str   # "ok" | "failed" | "skipped"
    detail: str


@dataclass
class BuildReport:
    stages: list[StageResult] = field(default_factory=list)
    track_path: str | None = None
    harden: HardenReport | None = None

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.stages)

    def _add(self, name: str, status: str, detail: str) -> StageResult:
        s = StageResult(name=name, status=status, detail=detail)
        self.stages.append(s)
        return s


def _canary(spec: Any, cfg: Any) -> None:
    """Stage 6 — the live canary. The seam for the live lane: one real run guarding
    fixture drift before promotion. Not built here (needs keys + network)."""
    raise NotImplementedError(
        "the live canary (stage 6) is the live lane — it needs keys + network and is the "
        "next increment. Validate manually for now with `zu run <agent>` (live), then "
        "promote with `zu pack` / `zu deploy`."
    )


async def build_offline(
    spec: Any, cfg: Any, agent_dir: str | Path, bundle: Bundle, *, min_score: float = 1.0,
) -> BuildReport:
    """Run the offline spine — build → record track → harden — and write the hardened
    track. Each stage gates the next: a failed offline build is not tracked, and a track
    that fails the resilience gate is recorded but flagged failed so promotion is held."""
    from zu_core.contracts import Status
    from zu_core.track import record_track

    report = BuildReport()

    # Stage 3 — build offline (the keystone). A clean replay is the precondition.
    result, events = await replay_offline(spec, cfg, bundle)
    if result.status is not Status.SUCCESS:
        report._add("build", "failed",
                    f"offline run did not succeed ({result.status.value}: {result.reason})")
        return report
    report._add("build", "ok", f"offline run succeeded → {result.value}")

    # Stage 4 — record the track from the clean offline run.
    track = record_track(events, task=spec.query, model=bundle.model)
    track_path = str(Path(agent_dir) / "track.json")
    track.save(track_path)
    report.track_path = track_path
    climbs = sorted({s.tier for s in track.steps})
    tiers = (f"tiers {min(climbs)}→{max(climbs)}" if len(climbs) > 1
             else f"tier {climbs[0]}" if climbs else "no tools")
    report._add("track", "ok", f"recorded {len(track.steps)} steps ({tiers}) → {track_path}")

    # Stage 5 — harden: score the track against perturbed fixtures and gate on it.
    hr = await harden(spec, cfg, bundle)
    report.harden = hr
    score = hr.resilience
    if not hr.grounding_load_bearing:
        report._add("harden", "failed",
                    "a value-deletion control passed — grounding is not gating; the "
                    "resilience score is unreliable")
    elif score < min_score:
        report._add("harden", "failed",
                    f"resilience {score:.0%} below --min-score {min_score:.0%} "
                    f"({len(hr.findings)} brittle step(s) to fix)")
    else:
        report._add("harden", "ok",
                    f"resilience {score:.0%}; {len(hr.findings)} brittle step(s) noted")
    return report
