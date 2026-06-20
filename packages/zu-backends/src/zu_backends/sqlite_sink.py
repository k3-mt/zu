"""sqlite — the durable EventSink (build step 3).

The on-disk system of record. Configured per the SQLite guidance for an
append-only log that must not lose committed events:

  * **WAL** journal mode — readers never block the single appender.
  * **synchronous=FULL** — full ACID durability (a committed event survives
    power loss); NORMAL in WAL can roll back the last commit on crash.
  * **busy_timeout** — auto-retry instead of failing on transient locks.
  * a **single writer connection** — the inherent WAL single-writer model.

An event read back is identical to what was written: each row stores the
event's full JSON (through a payload codec; plaintext by default), and reads
rebuild it with ``Event.model_validate_json``. ``append`` is idempotent via
``ON CONFLICT(event_id) DO NOTHING`` — a retried publish never duplicates.
Large reads never materialise the whole log: ``stream`` pages by keyset
(``WHERE seq > ? ORDER BY seq LIMIT ?``), never OFFSET.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import AsyncIterator

from zu_core.codec import IdentityCodec, PayloadCodec, decode_payload, encode_payload
from zu_core.contracts import Event
from zu_core.eventstore import validate_filter

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,   -- idempotency key
    trace_id       TEXT NOT NULL,
    task_id        TEXT NOT NULL,
    parent_id      TEXT,
    type           TEXT NOT NULL,
    source         TEXT NOT NULL,
    data           BLOB NOT NULL           -- payload codec output; canonical record
);
CREATE INDEX IF NOT EXISTS idx_events_task  ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(type);
"""


def _aad(event_id: str, trace_id: str, task_id: str, type_: str, source: str) -> bytes:
    # Bind the row's indexed/plaintext columns into the AEAD associated data, so
    # the ciphertext can't be moved to another row AND none of the index columns
    # can be silently edited at rest (e.g. to hide a row from a `type` filter):
    # any change makes the payload fail to decrypt. \x1f (unit separator) is a
    # delimiter that cannot appear in a UUID or our type/source spellings.
    return "\x1f".join((event_id, trace_id, task_id, type_, source)).encode("utf-8")


class SqliteSink:
    name = "sqlite"

    def __init__(
        self,
        path: str = "./zu.db",
        *,
        codec: PayloadCodec | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = path
        # Single writer connection (WAL single-writer model). check_same_thread
        # off because every DB call runs on an ``asyncio.to_thread`` worker (so a
        # commit's fsync never blocks the event loop). A sqlite3 connection is not
        # safe for concurrent use, so every DB access is serialised by self._lock
        # — correct by construction across whatever worker thread runs the call,
        # not by the fragile convention "no await between execute and commit".
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        # Codec for new writes (default plaintext). Reads dispatch on each row's
        # version tag, so plaintext rows remain readable after enabling a cipher.
        self._codec: PayloadCodec = codec or IdentityCodec()
        self._registry: dict[int, PayloadCodec] = {0: IdentityCodec()}
        self._registry[self._codec.version] = self._codec

    async def append(self, event: Event) -> None:
        event_id = str(event.event_id)
        aad = _aad(event_id, str(event.trace_id), str(event.task_id), event.type, event.source)
        blob = encode_payload(self._codec, event.model_dump_json(), aad)
        # The sqlite3 calls are synchronous and ``synchronous=FULL`` makes each
        # commit an fsync — seconds under load. Run off the event loop so a write
        # never blocks the loop (and every other coroutine: the bus awaits this
        # append before fan-out, so a blocking write would stall SSE streams and
        # all in-flight requests under ``zu serve``). The lock still serialises
        # the single connection across whatever worker thread runs the call.
        await asyncio.to_thread(self._sync_append, event, event_id, blob)

    def _sync_append(self, event: Event, event_id: str, blob: bytes) -> None:
        # Idempotent: a duplicate event_id is a no-op (scoped to that constraint).
        with self._lock:
            self._conn.execute(
                "INSERT INTO events "
                "(event_id, trace_id, task_id, parent_id, type, source, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (
                    event_id,
                    str(event.trace_id),
                    str(event.task_id),
                    str(event.parent_id) if event.parent_id is not None else None,
                    event.type,
                    event.source,
                    blob,
                ),
            )
            self._conn.commit()

    def _where(self, flt: dict) -> tuple[str, list]:
        validate_filter(flt)
        clauses: list[str] = []
        params: list = []
        for key, value in flt.items():
            if value is None:
                clauses.append(f"{key} IS NULL")  # column from allowlist
            else:
                clauses.append(f"{key} = ?")
                params.append(str(value))
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        aad = _aad(row["event_id"], row["trace_id"], row["task_id"], row["type"], row["source"])
        plaintext = decode_payload(row["data"], aad, self._registry)
        return Event.model_validate_json(plaintext)

    async def query(
        self, flt: dict | None = None, *, limit: int | None = None, after_seq: int = 0
    ) -> list[Event]:
        where, params = self._where(flt or {})
        sql = (
            "SELECT event_id, trace_id, task_id, type, source, data "
            f"FROM events WHERE seq > ?{where} ORDER BY seq ASC"
        )
        args: list = [after_seq, *params]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = await asyncio.to_thread(self._sync_fetchall, sql, args)
        return [self._row_to_event(r) for r in rows]

    def _sync_fetchall(self, sql: str, args: list) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    async def stream(
        self, flt: dict | None = None, *, batch_size: int = 500
    ) -> AsyncIterator[Event]:
        # Keyset pagination on seq — O(log n) per page, never OFFSET, never
        # fetchall. Memory is bounded by batch_size regardless of log size.
        where, base_params = self._where(flt or {})
        sql = (
            "SELECT seq, event_id, trace_id, task_id, type, source, data FROM events "
            f"WHERE seq > ?{where} ORDER BY seq ASC LIMIT ?"
        )
        after = 0
        while True:
            rows = await asyncio.to_thread(
                self._sync_fetchall, sql, [after, *base_params, batch_size]
            )
            if not rows:
                return
            for r in rows:
                after = r["seq"]
                yield self._row_to_event(r)
            if len(rows) < batch_size:
                return

    async def count(self, flt: dict | None = None) -> int:
        where, params = self._where(flt or {})
        sql = f"SELECT COUNT(*) AS n FROM events WHERE 1=1{where}"
        row = (await asyncio.to_thread(self._sync_fetchall, sql, params))[0]
        return int(row["n"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


__all__ = ["SqliteSink"]
