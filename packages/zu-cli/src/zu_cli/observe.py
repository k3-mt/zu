"""The uniform observability hook — wired the same way by every harness.

"Show me what this agent is doing, and what its guards just blocked" should be
identical whether you ``zu run``, embed ``import zu``, ``zu serve``, drive it over
MCP, or run the red-team gate. So each harness builds its bus and then calls
``attach_observability(bus, cfg.observability)`` — one place, one behaviour. The
taps it wires:

  * a live trace (the console train of thought), and
  * a defense review queue: every ``harness.defense.blocked`` event (a contained
    attack) is appended to a JSONL file, marked ``pending``, so a blocked attempt
    is visible and triageable in test AND in production — never a silent log line.

It is all read-side: pure subscribers on the bus, capability-free, isolated by
append-before-notify. Observation never participates in a run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from zu_core import events as ev


def defense_record(event: Any) -> dict:
    """The review-queue record for a contained attempt: the defense payload plus
    provenance (ts, ids) and ``status: pending`` for triage. Shared with the
    HTTP server so the queue shape is identical everywhere."""
    payload = getattr(event, "payload", {}) or {}
    return {
        **payload,
        "ts": event.ts.isoformat() if hasattr(event.ts, "isoformat") else str(event.ts),
        "trace_id": str(event.trace_id),
        "event_id": str(event.event_id),
        "status": "pending",
    }


def _review_tee(path: str) -> Callable[[Any], None]:
    """A subscriber that appends each defense event to the JSONL review queue.
    Queue IO never breaks a run (append-only, errors swallowed)."""

    def _on(event: Any) -> None:
        if getattr(event, "type", "") != ev.DEFENSE_BLOCKED:
            return
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(defense_record(event), default=str) + "\n")
        except OSError:
            pass

    return _on


def attach_observability(
    bus: Any, observability: Any, *, trace: bool = False, write: Callable[[str], None] | None = None
) -> None:
    """Wire the standard observability taps onto ``bus``. ``observability`` is the
    config block (``review_queue``: a JSONL path or None; ``scope``). ``trace``
    turns on the live console trace (the CLI sets it; embedding leaves it off)."""
    if trace:
        from .trace import live_printer

        bus.subscribe(live_printer(write))
    path = getattr(observability, "review_queue", None)
    if path:
        bus.subscribe(_review_tee(path))
