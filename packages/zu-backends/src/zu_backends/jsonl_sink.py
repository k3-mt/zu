"""jsonl — an append-only EventSink that writes one JSON object per line.

The pragmatic trace sink: human-readable, greppable, and exactly what log
shippers (Vector, Fluent Bit, Loki, an S3/GCS sidecar) tail. Point it at a local
path or a mounted cloud volume and the run's events flow there with no extra
infrastructure.

It is a full ``EventSink`` (so it can also be the canonical store), but its usual
home is a *secondary destination* on the bus (``add_destination``): the canonical
store stays the source of truth, and this ships a copy, isolated — a failure here
never breaks a run. As a shipper it is append-only (the bus fans out each event
once, after the idempotent canonical write), so it does not de-dup by event_id.

There is no DB sequence here, so reads treat the **1-based line ordinal** as the
sequence: ``query(after_seq=n)`` returns events written after the n-th line.
"""

from __future__ import annotations

import os
import threading
from typing import Any, AsyncIterator

from zu_core.contracts import Event
from zu_core.eventstore import event_matches, validate_filter


class JsonlSink:
    name = "jsonl"

    def __init__(self, path: str = "./zu-trace.jsonl") -> None:
        self.path = path
        # Serialise writes so concurrent appends never interleave a line.
        self._lock = threading.Lock()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def append(self, event: Any) -> None:
        line = event.model_dump_json()
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _read(self) -> list[Event]:
        if not os.path.exists(self.path):
            return []
        events: list[Event] = []
        with self._lock:
            with open(self.path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if raw:
                        events.append(Event.model_validate_json(raw))
        return events

    def _filtered(self, flt: dict | None) -> list[Event]:
        if flt:
            validate_filter(flt)
            return [e for e in self._read() if event_matches(e, flt)]
        return self._read()

    async def query(
        self, flt: dict | None = None, *, limit: int | None = None, after_seq: int = 0
    ) -> list[Event]:
        events = self._filtered(flt)[after_seq:]
        return events[:limit] if limit is not None else events

    async def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Event]:
        for event in self._filtered(flt):
            yield event

    async def count(self, flt: dict | None = None) -> int:
        return len(self._filtered(flt))
