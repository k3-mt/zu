"""Build step 3 — the SQLite EventSink.

Proves the event log is a faithful system of record: an event read back is
*identical* to what was written (nothing lost or mangled), queries filter and
order correctly, the filter is injection-safe, and the log survives across
connections.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from zu_core.contracts import Event
from zu_backends.sqlite_sink import SqliteSink


def _event(task_id, type="harness.task.started", **payload) -> Event:
    return Event(
        trace_id=uuid4(),
        task_id=task_id,
        type=type,
        source="loop",
        payload=payload or {"k": "v", "n": 1, "nested": {"a": [1, 2, 3]}},
    )


async def test_read_back_is_identical() -> None:
    sink = SqliteSink(":memory:")
    ev = _event(uuid4())
    await sink.append(ev)
    back = await sink.query({"task_id": ev.task_id})
    assert len(back) == 1
    assert back[0] == ev  # full equality: id, ts (tz-aware), payload, everything


async def test_query_filters_and_orders_by_insertion() -> None:
    sink = SqliteSink(":memory:")
    t1, t2 = uuid4(), uuid4()
    a = _event(t1, type="harness.task.started")
    b = _event(t2, type="harness.turn.started")
    c = _event(t1, type="harness.task.completed")
    for ev in (a, b, c):
        await sink.append(ev)

    only_t1 = await sink.query({"task_id": t1})
    assert [e.event_id for e in only_t1] == [a.event_id, c.event_id]  # order preserved

    by_type = await sink.query({"type": "harness.turn.started"})
    assert [e.event_id for e in by_type] == [b.event_id]

    assert len(await sink.query()) == 3  # no filter -> everything


async def test_query_rejects_unknown_filter() -> None:
    sink = SqliteSink(":memory:")
    with pytest.raises(ValueError):
        await sink.query({"payload": "anything"})  # not on the allowlist


async def test_persists_across_connections(tmp_path) -> None:
    db = str(tmp_path / "zu.db")
    ev = _event(uuid4())
    s1 = SqliteSink(db)
    await s1.append(ev)
    s1.close()

    s2 = SqliteSink(db)  # fresh connection, same file
    back = await s2.query({"task_id": ev.task_id})
    s2.close()
    assert back == [ev]
