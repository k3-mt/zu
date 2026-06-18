"""sqlite — the default EventSink (build step 3).

The local, append-only system of record. The bus appends here *before*
notifying any subscriber, so the log is the source of truth and a crashing
subscriber can never lose a record. An event read back is identical to the one
written: each row stores the event's full JSON, and ``query`` rebuilds it with
``Event.model_validate_json`` — so the round trip is lossless by construction.
Indexed columns (event_id / trace_id / task_id / type) exist only for
filtering; they never carry the canonical value.

OTel and central-log shippers arrive later as *additional* bus subscribers,
never changing this emitter — the canonical log is already here.
"""

from __future__ import annotations

import sqlite3

from zu_core.contracts import Event

# Only these columns may be filtered on. The allowlist is what keeps the
# WHERE-clause construction injection-safe: column names come from here (never
# from caller input) and all values are bound as parameters.
_ALLOWED_FILTERS = frozenset(
    {"event_id", "trace_id", "task_id", "parent_id", "type", "source"}
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL,
    trace_id       TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    parent_id      TEXT,
    type           TEXT NOT NULL,
    source         TEXT NOT NULL,
    data           TEXT NOT NULL   -- the event's full JSON; the canonical record
);
CREATE INDEX IF NOT EXISTS idx_events_task  ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(type);
"""


class SqliteSink:
    name = "sqlite"

    def __init__(self, path: str = "./zu.db") -> None:
        self.path = path
        # check_same_thread=False so the sink can be shared across the worker
        # threads an async runtime may use; access is serialized by the event
        # loop (append/query do no internal await, so they run atomically).
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    async def append(self, event: Event) -> None:
        self._conn.execute(
            "INSERT INTO events "
            "(event_id, trace_id, task_id, parent_id, type, source, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(event.event_id),
                str(event.trace_id),
                str(event.task_id),
                str(event.parent_id) if event.parent_id is not None else None,
                event.type,
                event.source,
                event.model_dump_json(),
            ),
        )
        self._conn.commit()

    async def query(self, flt: dict | None = None) -> list[Event]:
        flt = flt or {}
        clauses: list[str] = []
        params: list[str] = []
        for key, value in flt.items():
            if key not in _ALLOWED_FILTERS:
                raise ValueError(
                    f"unknown filter field: {key!r}; allowed: {sorted(_ALLOWED_FILTERS)}"
                )
            clauses.append(f"{key} = ?")  # column from allowlist; value bound below
            params.append(str(value))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT data FROM events{where} ORDER BY seq ASC", params
        ).fetchall()
        # Rebuilt from the stored JSON -> identical to what was written.
        return [Event.model_validate_json(row["data"]) for row in rows]

    def close(self) -> None:
        self._conn.close()
