"""In-container construction entrypoint — the autonomous brain, contained.

The production form of the meta-agent (the headline) is *zu's own ``construct()`` loop run
INSIDE the hardened container* ``SandboxLauncher`` builds — not an external CLI binary. The
meta-agent is just another contained zu run: caps dropped, blocking seccomp, and its only
egress the model endpoint (construction is offline except the strategist's model calls).
That reuses everything — the offline spine (build → record track → harden), the
``LiveStrategist`` brain, the anti-hardcode guardrails, cost telemetry, and the event log
(so the meta-agent's every step is observable) — instead of bolting on a binary that drives
zu over stdio and reasons outside the log.

Two halves, like ``zu_cli.sandbox``:

* :func:`construct_contained_from_env` — the in-container entrypoint (console script
  ``zu-construct-contained``). Reads the mounted agent, runs construction, and writes one
  JSON object (the report + the hardened track it produced) on stdout.
* the host-side launcher (the next increment) execs this inside the same hardened container,
  with the model endpoint on the egress allowlist, and parses the report back.

:func:`run_contained_construction` is the testable core — it runs the loop on a *writable
copy* of the agent (the bundle is mounted read-only, but the offline spine writes
``track.json``) with no Docker and no env, so the orchestration is verified the way the rest
of zu is: fakes/scripted providers, offline, ~$0.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def _report_to_dict(report: Any, track_text: str | None) -> dict:
    """Project a ConstructionReport into a JSON-able payload — convergence, each round's
    outcome, the violations still standing, and (only if it converged) the hardened track
    contents to write back. The stdout contract the host-side launcher parses."""
    guards = report.final_guardrails
    return {
        "ok": True,
        "converged": report.converged,
        "ready": report.converged,  # converged == build clean AND guardrails passed (G1–G3)
        "rounds": [
            {"round": r.round, "build_ok": r.build_ok,
             "guardrails_passed": r.guardrails_passed, "note": r.note}
            for r in report.rounds
        ],
        "violations": [
            {"rule": v.rule, "detail": v.detail} for v in (guards.violations if guards else [])
        ],
        "resilience": guards.resilience if guards else None,
        # The deliverable, handed back for review (G4 — never auto-promoted): the hardened
        # track.json, present only when construction converged.
        "track": track_text,
    }


async def _run(agent_dir: str | Path, *, max_rounds: int, min_resilience: float) -> dict:
    from .config import build_provider, load_agent
    from .construct import LiveStrategist, construct
    from .offline import Bundle, bundle_path

    src = Path(agent_dir)
    # Work on a WRITABLE copy: the bundle is mounted read-only in the container, but the
    # offline spine writes track.json. Skip prior runtime artifacts so the copy is clean.
    with tempfile.TemporaryDirectory(prefix="zu-construct-") as tmp:
        work = Path(tmp) / "agent"
        shutil.copytree(src, work, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))
        spec, cfg = load_agent(str(work))
        bundle = Bundle.load(bundle_path(work))
        # The brain is the agent's configured model (the frontier model in production); the
        # offline replay ignores it and replays the bundle, so the model is spent only on
        # the strategist's edits — the one thing that needs egress.
        provider = build_provider(cfg.provider)
        report = await construct(
            spec, cfg, str(work), bundle, LiveStrategist(provider),
            max_rounds=max_rounds, min_resilience=min_resilience,
        )
        track = work / "track.json"
        track_text = (track.read_text(encoding="utf-8")
                      if report.converged and track.is_file() else None)
        return _report_to_dict(report, track_text)


def run_contained_construction(
    agent_dir: str | Path, *, max_rounds: int = 3, min_resilience: float = 1.0
) -> dict:
    """Run the ``construct()`` loop on a writable copy of ``agent_dir`` and return a JSON-able
    report (convergence, rounds, remaining violations, resilience, and the hardened track if
    it converged). The testable core of the contained entrypoint — no Docker, no env."""
    return asyncio.run(_run(agent_dir, max_rounds=max_rounds, min_resilience=min_resilience))


def construct_contained_from_env(argv: list[str] | None = None) -> int:
    """Console-script entrypoint (``zu-construct-contained``) executed INSIDE the container.
    Reads the mounted agent at ``ZU_BUNDLE`` (and optional ``ZU_CONSTRUCT_MAX_ROUNDS`` /
    ``ZU_CONSTRUCT_MIN_RESILIENCE``), runs construction, and emits the report JSON on stdout
    — the same stdout-projection contract as ``run_contained_from_env``."""
    bundle = os.environ.get("ZU_BUNDLE")
    if not bundle:
        json.dump({"ok": False, "error": "ZU_BUNDLE (the mounted agent dir) is not set"},
                  sys.stdout)
        sys.stdout.write("\n")
        return 1
    # The mounted bundle carries its own gitignored .env (the brain's model key); load it so
    # the strategist's model is reachable inside the box.
    from .config import load_dotenv

    load_dotenv(Path(bundle) / ".env")
    max_rounds = int(os.environ.get("ZU_CONSTRUCT_MAX_ROUNDS", "3"))
    min_resilience = float(os.environ.get("ZU_CONSTRUCT_MIN_RESILIENCE", "1.0"))
    payload = run_contained_construction(
        bundle, max_rounds=max_rounds, min_resilience=min_resilience)
    json.dump(payload, sys.stdout, default=str)
    sys.stdout.write("\n")
    return 0


async def launch_contained_construction(
    launcher: Any, agent_dir: str | Path, *, allowlist: list[str],
    max_rounds: int = 3, min_resilience: float = 1.0,
) -> dict:
    """Run autonomous construction INSIDE the hardened box — the host-side half. Execs the
    ``zu-construct-contained`` entrypoint via ``launcher.run_entrypoint`` (a
    :class:`~zu_cli.sandbox.SandboxLauncher`), with the agent mounted read-only at
    ``/bundle`` and egress limited to ``allowlist`` (the model endpoint — construction is
    otherwise offline). Returns the construction report the entrypoint emitted: convergence,
    each round, the standing violations, and the hardened ``track.json`` contents to write
    back for review. Never auto-promotes (G4)."""
    return await launcher.run_entrypoint(
        ["zu-construct-contained"],
        {"ZU_CONSTRUCT_MAX_ROUNDS": str(max_rounds),
         "ZU_CONSTRUCT_MIN_RESILIENCE": str(min_resilience)},
        allowlist=allowlist, bundle_dir=str(agent_dir),
    )
