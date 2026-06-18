"""The in-memory default EventSink — the canonical store when none is configured.

This is the single source of truth for an ephemeral/dev run: the bus writes
here first, and reads delegate here. It holds the log in one place (no second
mirror), dedupes by ``event_id`` for idempotent append, and streams via a
generator so iteration never materialises the whole log. For durability or to
bound memory on disk, configure a persistent sink (e.g. zu-backends' SQLite)
instead — same port, same semantics.
"""

from __future__ import annotations

from typing import AsyncIterator

from .contracts import Event
from .eventstore import event_matches, validate_filter


class MemoryEventSink:
    name = "memory"

    def __init__(self) -> None:
        self._events: list[Event] = []  # insertion order == seq (1-based)
        self._seen: set = set()  # event_ids, for idempotency

    async def append(self, event: Event) -> None:
        if event.event_id in self._seen:
            return  # idempotent: re-appending the same event is a no-op
        self._seen.add(event.event_id)
        self._events.append(event)

    async def query(
        self, flt: dict | None = None, *, limit: int | None = None, after_seq: int = 0
    ) -> list[Event]:
        flt = flt or {}
        validate_filter(flt)
        out: list[Event] = []
        for seq, event in enumerate(self._events, start=1):
            if seq <= after_seq:
                continue
            if event_matches(event, flt):
                out.append(event)
                if limit is not None and len(out) >= limit:
                    break
        return out

    async def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Event]:
        flt = flt or {}
        validate_filter(flt)
        # A generator already yields one at a time — never builds the full list.
        for event in self._events:
            if event_matches(event, flt):
                yield event

    async def count(self, flt: dict | None = None) -> int:
        flt = flt or {}
        validate_filter(flt)
        return sum(1 for event in self._events if event_matches(event, flt))
