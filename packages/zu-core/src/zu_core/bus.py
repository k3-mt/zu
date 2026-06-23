"""The event bus — one source of truth, projected to destinations (step 3).

There is exactly **one canonical event store** (an ``EventSink``) — the single
source of truth for a run. The bus, on every publish:

  1. **appends to the canonical store first** — durability before any side
     effect. If that write fails, the failure propagates: you cannot have a run
     whose source of truth is missing a record.
  2. **then fans out to destinations** — projections (derived read models like
     the session store) and secondary sinks (a shipper to OTel, a central log).
     Each destination is isolated: one that raises does not stop the others,
     and its failure is recorded (bounded) rather than disappearing.

The canonical store defaults to an in-memory sink and is swapped for a durable
one (SQLite, Postgres, the hosted central log) by configuration — same port,
same semantics. Reads (`query`/`stream`/`count`) delegate to the canonical
store, so there is never a second, divergent copy of the log in the bus.
"""

from __future__ import annotations

import inspect
import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NamedTuple

from .contracts import Event
from .ports import EventSink
from .sinks import MemoryEventSink

log = logging.getLogger("zu.bus")

Subscriber = Callable[[Event], "Awaitable[None] | None"]


class SubscriberFailure(NamedTuple):
    """A destination that raised while handling an event — recorded, not lost."""

    subscriber: Subscriber
    event: Event
    error: Exception


class EventBus:
    def __init__(
        self,
        sink: EventSink | None = None,
        *,
        max_recorded_failures: int = 1000,
    ) -> None:
        # The single source of truth. Defaults to in-memory; configure a durable
        # sink for production. Never accompanied by a second in-bus copy.
        self.sink: EventSink = sink if sink is not None else MemoryEventSink()
        self._subscribers: list[Subscriber] = []
        # Secondary sinks attached via ``add_destination`` — tracked so ``aclose``
        # can release their resources (e.g. a sqlite connection) too.
        self._destinations: list[EventSink] = []
        # Bounded so a long-lived bus can't leak memory via recorded failures.
        self.subscriber_failures: deque[SubscriberFailure] = deque(
            maxlen=max_recorded_failures
        )

    def subscribe(self, fn: Subscriber) -> None:
        """Register a destination: a projection or any per-event handler."""
        self._subscribers.append(fn)

    def add_destination(self, sink: EventSink) -> None:
        """Project the stream to a secondary sink (e.g. a shipper), isolated.

        The secondary sink is a destination, not the source of truth: its
        failures are isolated like any other subscriber's, never propagated.
        """

        async def _ship(event: Event) -> None:
            await sink.append(event)

        self._destinations.append(sink)
        self.subscribe(_ship)

    async def aclose(self) -> None:
        """Release the canonical store and every secondary destination that holds
        a resource (e.g. a sqlite connection). ``close`` is an optional capability
        on a sink — a sink without one (the in-memory default, the per-append
        jsonl sink) is simply skipped. Each close is isolated so one failure does
        not strand the others. Idempotent: safe to call more than once.

        The embed facade assembles a fresh bus per run, so calling this in a
        ``finally`` is what keeps a long-lived ``Zu`` instance from leaking one
        connection per ``run()``."""
        for sink in [self.sink, *self._destinations]:
            closer = getattr(sink, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - one close failure must not strand the rest
                log.warning("sink %r failed to close: %s", sink, exc)

    async def publish(self, event: Event) -> None:
        # 1. canonical store first; a failure here propagates (source of truth).
        #    The canonical store links the event into its trace's hash chain
        #    (ZU-AUDIT-1) and returns the linked copy; fan THAT out so every
        #    shipper records the same hashes (link once, not per-sink). A sink
        #    that returns None (does not link) leaves the input event as-is.
        stored = await self.sink.append(event)
        if stored is not None:
            event = stored

        # 2. fan out to destinations, isolating any crash.
        for fn in self._subscribers:
            try:
                result = fn(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - one crash must not stop the rest
                self.subscriber_failures.append(SubscriberFailure(fn, event, exc))
                log.warning("destination %r failed on %s: %s", fn, event.type, exc)

    # --- reads delegate to the single source of truth ---------------------

    async def query(
        self, flt: dict | None = None, *, limit: int | None = None, after_seq: int = 0
    ) -> list[Event]:
        return await self.sink.query(flt, limit=limit, after_seq=after_seq)

    def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Event]:
        return self.sink.stream(flt, batch_size=batch_size)

    async def count(self, flt: dict | None = None) -> int:
        return await self.sink.count(flt)
