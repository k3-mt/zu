"""Defense logging — turn a contained attempt into a reviewable queue item.

A guard that refuses an action emits ``harness.defense.blocked`` onto the log
(see `zu_core.security.SecurityBlock` and the loop). :class:`DefenseMonitor` is a
bus subscriber that copies every such event into a **review queue** (any
``EventSink`` — a JSONL file in practice), marked ``pending``, so a blocked
attempt is never just a log line scrolling past: it is queued for a human to
review the defense. The same monitor runs in the gate and live in production
(`zu serve`), because the event it keys on exists in both.
"""

from __future__ import annotations

from typing import Any

from zu_core import events as ev
from zu_core.contracts import Event
from zu_core.ports import EventSink


class DefenseMonitor:
    """Subscribe to a run's bus; tee each defense event to the review queue.

    The queue record keeps the original ``event_id`` (so it links back to the
    canonical log) and adds ``status: "pending"`` for triage. Idempotent: the
    queue sink dedupes by ``event_id``, so a retried publish never double-queues.
    """

    def __init__(self, queue: EventSink) -> None:
        self._queue = queue
        self.blocked: list[Event] = []  # in-memory view for the current process

    async def __call__(self, event: Event) -> None:
        if event.type != ev.DEFENSE_BLOCKED:
            return
        record = event.model_copy(update={"payload": {**event.payload, "status": "pending"}})
        self.blocked.append(record)
        await self._queue.append(record)


def monitor_defenses(bus: Any, queue: EventSink) -> DefenseMonitor:
    """Attach a :class:`DefenseMonitor` to ``bus``, writing to ``queue``. Returns
    the monitor (its ``.blocked`` list is the live in-process view)."""
    monitor = DefenseMonitor(queue)
    bus.subscribe(monitor)
    return monitor
