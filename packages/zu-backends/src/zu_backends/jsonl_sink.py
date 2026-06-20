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

import asyncio
import os
import threading
from collections.abc import AsyncIterator
from typing import IO, Any

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
        # File I/O is blocking and the bus awaits this append before fan-out, so
        # writing directly on the event loop would stall every in-flight request
        # and SSE stream under ``zu serve`` (the exact pitfall SqliteSink offloads
        # to a thread for). Run it on a worker thread for the same reason.
        await asyncio.to_thread(self._sync_append, line)

    def _sync_append(self, line: str) -> None:
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
        # query() returns a materialised list by contract; do the blocking read
        # off the loop so it doesn't stall other coroutines.
        events = (await asyncio.to_thread(self._filtered, flt))[after_seq:]
        return events[:limit] if limit is not None else events

    def _read_batch(self, fh: IO[str], batch_size: int) -> list[Event]:
        """Parse up to ``batch_size`` non-blank lines from the open handle, in a
        worker thread — so both the read and the JSON parse stay off the loop."""
        out: list[Event] = []
        for raw in fh:
            raw = raw.strip()
            if raw:
                out.append(Event.model_validate_json(raw))
                if len(out) >= batch_size:
                    break
        return out

    async def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Event]:
        # Genuinely bound memory to one ``batch_size`` page: read and parse the
        # file incrementally on a worker thread rather than materialising the
        # whole log (the previous ``_filtered`` read the entire file first,
        # ignoring batch_size and defeating the streaming contract).
        if flt is not None:
            validate_filter(flt)
        if not os.path.exists(self.path):
            return
        fh = await asyncio.to_thread(open, self.path, "r", encoding="utf-8")
        try:
            while True:
                batch = await asyncio.to_thread(self._read_batch, fh, batch_size)
                if not batch:
                    return
                for event in batch:
                    if flt is None or event_matches(event, flt):
                        yield event
                if len(batch) < batch_size:
                    return
        finally:
            await asyncio.to_thread(fh.close)

    async def count(self, flt: dict | None = None) -> int:
        return len(await asyncio.to_thread(self._filtered, flt))
