"""Projections — derived, rebuildable views over the event log.

A projection is a bus subscriber that folds events into a view. It must stay
small: the full history lives in the canonical store (the single source of
truth) and is read back with ``bus.query`` / ``bus.stream``; a projection holds
only what's cheap to keep in memory and expensive to recompute on the hot path.

``SessionStore`` (the design's "session store as its first projection") is the
in-pod session state the interpreter loop reads as a run progresses. It keeps
**compact per-task facts** — turn count, event count, last event, optional
recent window — so its memory is O(active tasks), not O(events), and it evicts
a task's state when the task reaches a terminal event (configurable). For a
task's full history, query the store, not this projection.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from uuid import UUID

from . import events as event_types
from .contracts import Event

_TERMINAL_TYPES = frozenset(
    {event_types.TASK_COMPLETED, event_types.TASK_TERMINAL}
)


@dataclass
class SessionState:
    """Compact per-task facts — never the full event list."""

    event_count: int = 0
    turn_count: int = 0
    last: Event | None = None
    recent: deque[Event] = field(default_factory=lambda: deque(maxlen=20))


class SessionStore:
    def __init__(self, *, evict_on_terminal: bool = False, recent_window: int = 20) -> None:
        # evict_on_terminal: drop a task's state as soon as it finishes. Default
        # off so the loop can read final state; the loop calls evict() when done.
        self._evict_on_terminal = evict_on_terminal
        self._recent_window = recent_window
        self._by_task: dict[UUID, SessionState] = {}

    def __call__(self, event: Event) -> None:
        state = self._by_task.get(event.task_id)
        if state is None:
            state = SessionState(recent=deque(maxlen=self._recent_window))
            self._by_task[event.task_id] = state
        state.event_count += 1
        if event.type == event_types.TURN_STARTED:
            state.turn_count += 1
        state.last = event
        state.recent.append(event)
        if self._evict_on_terminal and event.type in _TERMINAL_TYPES:
            self._by_task.pop(event.task_id, None)

    # --- compact accessors (full history comes from the canonical store) ---

    def state(self, task_id: UUID) -> SessionState | None:
        return self._by_task.get(task_id)

    def event_count(self, task_id: UUID) -> int:
        state = self._by_task.get(task_id)
        return state.event_count if state else 0

    def turns(self, task_id: UUID) -> int:
        state = self._by_task.get(task_id)
        return state.turn_count if state else 0

    def last(self, task_id: UUID) -> Event | None:
        state = self._by_task.get(task_id)
        return state.last if state else None

    def recent(self, task_id: UUID) -> list[Event]:
        state = self._by_task.get(task_id)
        return list(state.recent) if state else []

    def tasks(self) -> list[UUID]:
        return list(self._by_task)

    def evict(self, task_id: UUID) -> None:
        self._by_task.pop(task_id, None)
