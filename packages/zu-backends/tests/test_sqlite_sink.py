"""Build step 3 — the SQLite EventSink.

Proves the event log is a faithful, durable system of record: an event read
back is identical to what was written; append is idempotent; queries filter
(incl. null parent), order, paginate, and stream without loading everything;
and the log survives across connections.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from zu_backends.sqlite_sink import SqliteSink
from zu_core.contracts import Event


def _event(task_id, type="harness.task.started", parent=None, **payload) -> Event:
    return Event(
        trace_id=uuid4(),
        task_id=task_id,
        parent_id=parent,
        type=type,
        source="loop",
        payload=payload or {"k": "v", "n": 1, "nested": {"a": [1, 2, 3]}},
    )


async def test_read_back_is_identical() -> None:
    sink = SqliteSink(":memory:")
    ev = _event(uuid4())
    stored = await sink.append(ev)  # the linked (hash-chained) event that persists
    assert stored is not None and stored.hash is not None  # ZU-AUDIT-1
    back = await sink.query({"task_id": ev.task_id})
    assert back == [stored]  # full equality: id, ts (tz-aware), payload, chain


async def test_append_is_idempotent() -> None:
    sink = SqliteSink(":memory:")
    ev = _event(uuid4())
    stored = await sink.append(ev)
    again = await sink.append(ev)  # ON CONFLICT(event_id) DO NOTHING
    assert await sink.count() == 1
    assert again == stored  # idempotent re-append returns the stored linked copy
    assert await sink.query({"event_id": ev.event_id}) == [stored]


async def test_filter_by_parent_id_null() -> None:
    sink = SqliteSink(":memory:")
    root = _event(uuid4(), parent=None)
    child = _event(root.task_id, parent=uuid4())
    await sink.append(root)
    await sink.append(child)
    roots = await sink.query({"parent_id": None})
    assert [e.event_id for e in roots] == [root.event_id]


async def test_query_orders_and_paginates() -> None:
    sink = SqliteSink(":memory:")
    task = uuid4()
    evs = [_event(task) for _ in range(5)]
    for e in evs:
        await sink.append(e)

    assert [e.event_id for e in await sink.query({"task_id": task})] == [
        e.event_id for e in evs
    ]
    first_two = await sink.query({"task_id": task}, limit=2)
    assert [e.event_id for e in first_two] == [evs[0].event_id, evs[1].event_id]
    after_two = await sink.query({"task_id": task}, after_seq=2)
    assert [e.event_id for e in after_two] == [e.event_id for e in evs[2:]]


async def test_stream_pages_without_loading_all() -> None:
    sink = SqliteSink(":memory:")
    task = uuid4()
    evs = [_event(task) for _ in range(10)]
    for e in evs:
        await sink.append(e)
    # batch_size smaller than the set forces multiple keyset pages
    streamed = [e async for e in sink.stream({"task_id": task}, batch_size=3)]
    assert [e.event_id for e in streamed] == [e.event_id for e in evs]


async def test_count() -> None:
    sink = SqliteSink(":memory:")
    t1, t2 = uuid4(), uuid4()
    await sink.append(_event(t1))
    await sink.append(_event(t1))
    await sink.append(_event(t2))
    assert await sink.count() == 3
    assert await sink.count({"task_id": t1}) == 2


async def test_query_rejects_unknown_filter() -> None:
    sink = SqliteSink(":memory:")
    with pytest.raises(ValueError):
        await sink.query({"payload": "anything"})


async def test_chain_persists_and_verifies_across_connections(tmp_path) -> None:
    # ZU-AUDIT-1: the per-trace hash chain survives a reopen and verifies; a new
    # connection seeds its head from the DB so the chain keeps extending.
    from zu_core.chain import verify_chain

    db = str(tmp_path / "chain.db")
    trace, task = uuid4(), uuid4()

    def ev() -> Event:
        return Event(trace_id=trace, task_id=task, type="harness.task.started", source="loop")

    s1 = SqliteSink(db)
    for _ in range(3):
        await s1.append(ev())
    s1.close()

    s2 = SqliteSink(db)
    await s2.append(ev())  # extends the chain after restart
    events = await s2.query({"trace_id": trace})
    s2.close()
    assert len(events) == 4
    assert verify_chain(events) == []


async def test_consumer_field_indexed_and_queryable() -> None:
    # ZU-AUDIT-3: a registered payload["ctx"] field is queryable via the side
    # index on the durable sink; an unregistered field is still rejected.
    from zu_core.eventstore import register_event_filter

    register_event_filter("consent_ref")
    sink = SqliteSink(":memory:")
    task = uuid4()
    await sink.append(_event(task, ctx={"consent_ref": "C-1"}))
    await sink.append(_event(task, ctx={"consent_ref": "C-2"}))
    await sink.append(_event(task, ctx={"consent_ref": "C-1"}))
    rows = await sink.query({"consent_ref": "C-1"})
    assert len(rows) == 2
    with pytest.raises(ValueError):
        await sink.query({"not_registered": "x"})


def test_concurrent_threads_do_not_corrupt(tmp_path) -> None:
    """The shared connection (check_same_thread=False) is serialised by an
    internal lock, so appends from many threads — each on its own event loop,
    the planned executor-offload case — don't race or corrupt the log."""
    import asyncio
    import threading

    db = str(tmp_path / "zu.db")
    sink = SqliteSink(db)
    per_thread, n_threads = 25, 8

    def worker() -> None:
        task = uuid4()
        for _ in range(per_thread):
            asyncio.run(sink.append(_event(task)))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert asyncio.run(sink.count()) == per_thread * n_threads
    sink.close()


async def test_persists_across_connections(tmp_path) -> None:
    db = str(tmp_path / "zu.db")
    ev = _event(uuid4())
    s1 = SqliteSink(db)
    stored = await s1.append(ev)
    s1.close()

    s2 = SqliteSink(db)  # fresh connection, same file
    back = await s2.query({"task_id": ev.task_id})
    s2.close()
    assert back == [stored]
