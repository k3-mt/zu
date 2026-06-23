"""ZU-AUDIT-1 — the event log is append-only AND tamper-evident.

Proves the per-trace hash chain: a clean log verifies; editing a payload,
deleting an event, or reordering events is detected on replay. ZU-AUDIT-3 — a
consumer-registered ``payload["ctx"]`` field is queryable on the in-memory sink.
"""

from __future__ import annotations

from uuid import uuid4

from zu_core.chain import verify_chain
from zu_core.contracts import Event
from zu_core.eventstore import register_event_filter
from zu_core.sinks import MemoryEventSink


def _event(trace, task, type="harness.task.started", **payload) -> Event:
    return Event(
        trace_id=trace,
        task_id=task,
        type=type,
        source="loop",
        payload=payload or {"k": "v"},
    )


async def _log(sink, trace, task, n=4) -> list[Event]:
    out = []
    for i in range(n):
        stored = await sink.append(_event(trace, task, i=i))
        out.append(stored)
    return out


async def test_clean_chain_verifies() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    assert all(e.hash is not None for e in events)
    assert events[0].prev_hash is None  # first event of the trace roots the chain
    assert verify_chain(events) == []  # intact


async def test_content_tamper_detected() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    # Edit a stored event's payload (e.g. hide what was actually done).
    events[2] = events[2].model_copy(update={"payload": {"k": "evil"}})
    violations = verify_chain(events)
    assert any("content tamper" in v for v in violations)


async def test_deletion_detected() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    # Drop the middle event: the next event's prev_hash no longer matches.
    pruned = events[:2] + events[3:]
    violations = verify_chain(pruned)
    assert any("prev_hash break" in v for v in violations)


async def test_reorder_detected() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    swapped = [events[0], events[2], events[1], events[3]]
    assert verify_chain(swapped) != []


async def test_chains_are_per_trace_independent() -> None:
    sink = MemoryEventSink()
    t1, t2, task = uuid4(), uuid4(), uuid4()
    # Interleave two traces; each chain must verify on its own.
    await sink.append(_event(t1, task, i=0))
    await sink.append(_event(t2, task, i=0))
    await sink.append(_event(t1, task, i=1))
    await sink.append(_event(t2, task, i=1))
    assert verify_chain(await sink.query({"trace_id": t1})) == []
    assert verify_chain(await sink.query({"trace_id": t2})) == []


async def test_consumer_field_is_queryable() -> None:
    # ZU-AUDIT-3: a consumer registers a payload["ctx"] field and filters on it.
    register_event_filter("grant_id")
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await sink.append(_event(trace, task, ctx={"grant_id": "G-1"}))
    await sink.append(_event(trace, task, ctx={"grant_id": "G-2"}))
    await sink.append(_event(trace, task, ctx={"grant_id": "G-1"}))
    rows = await sink.query({"grant_id": "G-1"})
    assert len(rows) == 2
    assert all(r.payload["ctx"]["grant_id"] == "G-1" for r in rows)
