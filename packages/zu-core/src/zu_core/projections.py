"""Projections — derived, rebuildable views over the event log.

A projection is just a bus subscriber that folds events into a view. The first
one (the design's "session store as its first projection") is the in-pod
session state the interpreter loop reads as a run progresses: the per-task
event history plus a few derived facts (turn count, last event). It holds
nothing the log doesn't — it could be rebuilt by replaying the log — which is
the whole point of event-sourcing.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from . import events as event_types
from .contracts import Event


class SessionStore:
    """Per-task event history + a small derived view. Subscribe it to a bus."""

    def __init__(self) -> None:
        self._by_task: dict[UUID, list[Event]] = defaultdict(list)

    def __call__(self, event: Event) -> None:
        self._by_task[event.task_id].append(event)

    def events(self, task_id: UUID) -> list[Event]:
        return list(self._by_task[task_id])

    def turns(self, task_id: UUID) -> int:
        return sum(
            1 for e in self._by_task[task_id] if e.type == event_types.TURN_STARTED
        )

    def last(self, task_id: UUID) -> Event | None:
        history = self._by_task[task_id]
        return history[-1] if history else None

    def tasks(self) -> list[UUID]:
        return list(self._by_task)
