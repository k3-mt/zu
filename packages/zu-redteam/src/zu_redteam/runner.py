"""``zu-redteam-run`` — the in-container scenario runner (RED_TEAM_CONTAINER.md §3.2).

Inside the target container, this runs one red-team scenario on **real Zu**
in-process and writes the canonical event log to stdout as JSONL — one event per
line. The control plane on the host reads that stdout for log (a), merges it with
the egress-proxy log (b) and the host-effect audit log (c), and judges the result
with the out-of-band observers (``ContainerGate``).

The scenario is described as plain JSON so it crosses the container boundary
without pickling: plugins are named by import path (``module:attr``) and
instantiated in the box, where the target package is installed. This is what lets
the real corpus run *inside* the container instead of being smoke-tested.

    echo '<spec.json>' | zu-redteam-run        # reads spec from stdin
    zu-redteam-run spec.json                    # or from a file argument
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from typing import Any

from zu_core.contracts import Event

from .harness import Scenario, run_scenario


def _load(path: str) -> Any:
    """Resolve a ``module:attr`` import path to the object (a plugin class/factory).
    Fails loudly — an unresolvable plugin is a broken spec, not a silent skip."""
    module, sep, attr = path.partition(":")
    if not sep:
        raise ValueError(f"plugin import {path!r} must be 'module:attr'")
    return getattr(importlib.import_module(module), attr)


def build_scenario(spec: dict) -> Scenario:
    """Reconstruct a :class:`Scenario` from a JSON spec. ``plugins`` is a list of
    ``{kind, name, import, args?}``; ``include_benign_neighbours`` adds the
    standard cross-category neighbours (deduped) so interop holds in the box."""
    plugins: list[tuple[str, str, Any]] = []
    for p in spec.get("plugins", []):
        factory = _load(p["import"])
        obj = factory(**(p.get("args") or {}))
        plugins.append((p["kind"], p["name"], obj))
    if spec.get("include_benign_neighbours"):
        from .fixtures import benign_neighbours

        present = {(k, n) for k, n, _ in plugins}
        plugins += [pl for pl in benign_neighbours() if (pl[0], pl[1]) not in present]
    return Scenario(
        objective=spec.get("objective", "container"),
        plugins=plugins,
        moves=spec.get("moves", []),
        query=spec.get("query", "Extract the requested data."),
        target=spec.get("target"),
        planted_secret=spec.get("planted_secret", ""),
        neighbours=spec.get("neighbours", []),
    )


async def run_spec(spec: dict) -> list[Event]:
    """Run one scenario spec on real Zu in-process and return its event log."""
    scenario = build_scenario(spec)
    observed = await run_scenario(scenario)
    return list(observed.events)


def events_to_jsonl(events: list[Event]) -> str:
    """Serialise an event log as JSONL — the wire form the container emits and the
    host parses back into events to build the ObservedRun."""
    return "\n".join(e.model_dump_json() for e in events)


def jsonl_to_events(text: str) -> list[Event]:
    """Parse JSONL stdout (from a container run) back into events, host-side."""
    return [Event.model_validate_json(line) for line in text.splitlines() if line.strip()]


def _read_spec(argv: list[str]) -> str:
    """The scenario spec, from (in order) a file argument, the ``ZU_REDTEAM_SPEC``
    env var (how the container backend passes it on exec), or stdin."""
    if argv:
        with open(argv[0]) as fh:
            return fh.read()
    if os.environ.get("ZU_REDTEAM_SPEC"):
        return os.environ["ZU_REDTEAM_SPEC"]
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    spec = json.loads(_read_spec(argv))
    events = asyncio.run(run_spec(spec))
    sys.stdout.write(events_to_jsonl(events) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    raise SystemExit(main())
