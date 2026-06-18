"""The event bus — append-before-notify (build step 3).

The bus is the spine. On every publish it:

  1. **appends to the EventSink first** — durability before any side effect, so
     the log is the source of truth; if the process dies mid-notify the record
     already exists.
  2. **then notifies every subscriber** (projections), isolating each one — a
     subscriber that raises does not stop the others, and its failure is
     recorded (on ``subscriber_failures``) rather than disappearing.

The bus depends only on the ``EventSink`` *port*, never on a concrete sink, so
SQLite, Postgres, or the hosted central log are all swappable behind it.
"""

from __future__ import annotations

import inspect
import logging
from typing import Awaitable, Callable, NamedTuple

from .contracts import Event
from .ports import EventSink

log = logging.getLogger("zu.bus")

Subscriber = Callable[[Event], "Awaitable[None] | None"]


class SubscriberFailure(NamedTuple):
    """A subscriber that raised while handling an event — recorded, not lost."""

    subscriber: Subscriber
    event: Event
    error: Exception


class EventBus:
    def __init__(self, sink: EventSink | None = None) -> None:
        self._sink = sink
        self._subscribers: list[Subscriber] = []
        self._log: list[Event] = []
        self.subscriber_failures: list[SubscriberFailure] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    async def publish(self, event: Event) -> None:
        # 1. append-before-notify: persist to the durable log first.
        if self._sink is not None:
            await self._sink.append(event)
        self._log.append(event)

        # 2. notify every subscriber, isolating any crash.
        for fn in self._subscribers:
            try:
                result = fn(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - one crash must not stop the rest
                self.subscriber_failures.append(SubscriberFailure(fn, event, exc))
                log.warning("subscriber %r failed on %s: %s", fn, event.type, exc)

    @property
    def log(self) -> list[Event]:
        return list(self._log)
