"""The in-memory default sink: idempotency, null filters, pagination, streaming."""

from __future__ import annotations

from uuid import uuid4

import pytest

from zu_core.contracts import Event
from zu_core.sinks import MemoryEventSink


def _event(task_id, type="harness.task.started", parent=None) -> Event:
    return Event(trace_id=uuid4(), task_id=task_id, parent_id=parent, type=type, source="loop")


async def test_append_is_idempotent() -> None:
    sink = MemoryEventSink()
    ev = _event(uuid4())
    await sink.append(ev)
    await sink.append(ev)  # same event_id -> no-op
    assert await sink.count() == 1


async def test_filter_by_parent_id_null() -> None:
    sink = MemoryEventSink()
    root = _event(uuid4(), parent=None)
    child = _event(root.task_id, parent=uuid4())
    await sink.append(root)
    await sink.append(child)

    roots = await sink.query({"parent_id": None})
    assert [e.event_id for e in roots] == [root.event_id]


async def test_query_limit_and_after_seq() -> None:
    sink = MemoryEventSink()
    task = uuid4()
    evs = [_event(task) for _ in range(5)]
    for e in evs:
        await sink.append(e)

    first_two = await sink.query({"task_id": task}, limit=2)
    assert [e.event_id for e in first_two] == [evs[0].event_id, evs[1].event_id]

    after_two = await sink.query({"task_id": task}, after_seq=2)
    assert [e.event_id for e in after_two] == [e.event_id for e in evs[2:]]


async def test_stream_yields_all_in_order() -> None:
    sink = MemoryEventSink()
    task = uuid4()
    evs = [_event(task) for _ in range(7)]
    for e in evs:
        await sink.append(e)
    streamed = [e async for e in sink.stream({"task_id": task})]
    assert [e.event_id for e in streamed] == [e.event_id for e in evs]


async def test_unknown_filter_rejected() -> None:
    sink = MemoryEventSink()
    with pytest.raises(ValueError):
        await sink.query({"payload": "nope"})
