"""The event bus — append-before-notify (build step 3).

The bus is the spine: it appends every event to the EventSink *before*
notifying subscribers (projections), so the log is the source of truth and a
crashing subscriber can never lose a record. The SQLite sink and the session
store projection arrive in build step 3; this module currently provides the
in-memory shell the loop will write through.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from .contracts import Event

Subscriber = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Append-before-notify dispatcher over a list of subscribers.

    A full implementation (step 3) persists to an EventSink before notifying,
    isolates subscriber crashes, and records the failure as its own event.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._log: list[Event] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    async def publish(self, event: Event) -> None:
        # append first…
        self._log.append(event)
        # …then notify. (Step 3: persist to sink, isolate subscriber failures.)
        for fn in self._subscribers:
            result = fn(event)
            if result is not None:
                await result

    @property
    def log(self) -> list[Event]:
        return list(self._log)
