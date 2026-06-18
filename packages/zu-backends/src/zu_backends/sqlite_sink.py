"""sqlite — the default EventSink (build step 3).

The local, append-only system of record. The bus appends here before
notifying subscribers, so an event read back is byte-for-byte what was
written. OTel and central-log shippers arrive later as additional
subscribers, never changing the emitters. Importable now; the SQLite schema
and append/query are wired in build step 3.
"""

from __future__ import annotations

from typing import Any

from zu_core.contracts import Event


class SqliteSink:
    name = "sqlite"

    def __init__(self, path: str = "./zu.db") -> None:
        self.path = path

    async def append(self, event: Event) -> None:
        raise NotImplementedError(
            "sqlite.append is build step 3: persist the event row "
            "(event_id, trace_id, task_id, parent_id, type, ts, source, "
            "payload JSON, schema_version) before any subscriber is notified."
        )

    async def query(self, flt: dict) -> list[Any]:
        raise NotImplementedError(
            "sqlite.query is build step 3: return events matching the filter, "
            "rebuilt identically to how they were written."
        )
