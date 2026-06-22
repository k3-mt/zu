"""Capability gaps → strong, reproducible issues.

zu's discipline is: when you hit a wall you don't hardcode around it — you build a GENERIC
capability (the model reasons, the tool exposes a primitive). This extends that to everyone
using zu. When a harness hits something zu genuinely can't do — a missing primitive, a
detector that won't fire, a selector zu can't resolve, a soft miss it mishandles — that's a
**capability gap in zu, not a bug in the user's agent**, and the fix belongs upstream.

The hard part of a good bug report is a reliable repro. Here it is **free**: a captured
``fixtures/`` bundle reproduces the run deterministically at $0, so the maintainers' agent can
``zu run --offline`` the attached bundle, reproduce the gap exactly, and build the generic
capability that closes it. This module turns a gap into that issue — agent config + the
repeatable example + expected/observed + a proposed generic capability — ready to file.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

# The repo a capability gap is contributed to. Overridable so a fork/mirror can retarget it.
ZU_REPO = os.environ.get("ZU_CONTRIBUTE_REPO", "k3-mt/zu")
GAP_LABEL = "capability-gap"


def _zu_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("zu-runtime", "zu-cli", "zu-core"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "unknown"


@dataclass
class GapReport:
    """A ready-to-file capability-gap issue: the ``title``, the markdown ``body``, and whether
    a deterministic ``fixtures/`` repro is attached (``has_repro``)."""

    title: str
    body: str
    has_repro: bool
    repro_path: str | None

    def gh_command(self, body_file: str, *, repo: str = ZU_REPO) -> str:
        """A ready ``gh issue create`` invocation (body passed by file, since it's multi-line
        and embeds YAML). The caller writes the body to ``body_file`` first."""
        return (f"gh issue create --repo {repo} --label {GAP_LABEL} "
                f"--title {shlex.quote(self.title)} --body-file {shlex.quote(body_file)}")


def build_gap_report(
    agent_dir: str | Path, *, summary: str, expected: str, observed: str,
    proposed: str | None = None, zu_version: str | None = None,
) -> GapReport:
    """Build a capability-gap issue for the agent at ``agent_dir``. Embeds the agent's
    ``agent.yaml`` and, if present, points at its ``fixtures/`` bundle as the **repeatable
    example** (reproduced with ``zu run --offline``). With no bundle the report still builds
    but flags that a repro must be captured first — a gap without a repro is hard to pick up."""
    from .offline import FIXTURES_DIR, bundle_path

    base = Path(agent_dir)
    cfg_text = ""
    for name in ("agent.yaml", "agent.yml"):
        p = base / name
        if p.is_file():
            cfg_text = p.read_text(encoding="utf-8")
            break
    repro = bundle_path(base)
    has_repro = repro.is_file()
    version = zu_version or _zu_version()
    title = f"Capability gap: {summary}"

    repro_section = (
        f"This agent ships `{FIXTURES_DIR}/capture.json` — a deterministic, $0 reproduction.\n"
        f"Reproduce the gap with **no model and no network**:\n\n"
        f"```\nzu run <agent> --offline\n```\n"
        if has_repro else
        "⚠️ **No fixtures bundle attached.** A capability gap needs a repeatable example so it "
        "can be picked up. Capture one first — drive the path with `zu_explore` (your harness) "
        "or `zu capture` (once, live) to record `fixtures/capture.json`, then re-run this.\n"
    )
    proposed_section = (
        f"## Proposed generic capability\n{proposed}\n\n" if proposed else
        "## Proposed generic capability\n_(none suggested — describe the smallest GENERIC "
        "primitive that would close this, in zu's no-hardcoding spirit.)_\n\n"
    )
    body = (
        f"## What I was building\n{summary}\n\n"
        f"## What I expected\n{expected}\n\n"
        f"## What zu did (the gap)\n{observed}\n\n"
        f"## Repeatable example\n{repro_section}\n"
        f"<details><summary>agent.yaml</summary>\n\n```yaml\n{cfg_text.rstrip()}\n```\n</details>\n\n"
        f"{proposed_section}"
        f"## Environment\n- zu {version}\n\n"
        f"---\n_Filed via `zu_report_gap`. The fix should be a generic capability (no "
        f"site-specific hardcoding); the attached bundle replays the gap deterministically._\n"
    )
    return GapReport(title=title, body=body, has_repro=has_repro,
                     repro_path=str(repro) if has_repro else None)
