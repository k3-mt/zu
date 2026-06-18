"""Build step 3 — the single-source-of-truth bus + the session-store projection.

Proves: the event is durably appended to the canonical store *before* any
destination is notified; a destination crash doesn't stop the others and is
recorded (bounded); the canonical store is the only copy (reads delegate to
it); and a sink failure propagates (the source of truth must not silently drop
a record).
"""

from __future__ import annotations

from typing import AsyncIterator
from uuid import uuid4

from zu_core import events as event_types
from zu_core.bus import EventBus
from zu_core.contracts import Event
from zu_core.projections import SessionStore
from zu_core.sinks import MemoryEventSink


class FakeSink:
    """Minimal EventSink exposing a sync view, to observe notify-time ordering."""

    def __init__(self) -> None:
        self.appended: list[Event] = []

    async def append(self, event: Event) -> None:
        self.appended.append(event)

    async def query(self, flt=None, *, limit=None, after_seq=0) -> list[Event]:
        return list(self.appended)

    async def stream(self, flt=None, *, batch_size=500) -> AsyncIterator[Event]:
        for ev in self.appended:
            yield ev

    async def count(self, flt=None) -> int:
        return len(self.appended)


class ExplodingSink:
    async def append(self, event: Event) -> None:
        raise RuntimeError("disk is on fire")

    async def query(self, flt=None, *, limit=None, after_seq=0) -> list[Event]:
        return []

    async def stream(self, flt=None, *, batch_size=500) -> AsyncIterator[Event]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def count(self, flt=None) -> int:
        return 0


def _event(task_id, type=event_types.TASK_STARTED) -> Event:
    return Event(trace_id=uuid4(), task_id=task_id, type=type, source="loop")


def test_default_sink_is_in_memory() -> None:
    bus = EventBus()
    assert isinstance(bus.sink, MemoryEventSink)


async def test_append_before_notify() -> None:
    sink = FakeSink()
    bus = EventBus(sink=sink)
    seen_in_sink_at_notify: list[list[Event]] = []
    bus.subscribe(lambda ev: seen_in_sink_at_notify.append(list(sink.appended)))

    ev = _event(uuid4())
    await bus.publish(ev)
    # the event was already durable in the canonical store before any notify
    assert seen_in_sink_at_notify == [[ev]]


async def test_every_destination_notified() -> None:
    bus = EventBus()
    got: list[str] = []
    bus.subscribe(lambda e: got.append("a"))
    bus.subscribe(lambda e: got.append("b"))
    bus.subscribe(lambda e: got.append("c"))
    await bus.publish(_event(uuid4()))
    assert sorted(got) == ["a", "b", "c"]


async def test_one_crashing_destination_doesnt_stop_the_rest() -> None:
    bus = EventBus()
    reached: list[str] = []

    def boom(ev: Event) -> None:
        raise RuntimeError("projection blew up")

    bus.subscribe(lambda e: reached.append("before"))
    bus.subscribe(boom)
    bus.subscribe(lambda e: reached.append("after"))

    ev = _event(uuid4())
    await bus.publish(ev)  # must not raise

    assert reached == ["before", "after"]
    assert len(bus.subscriber_failures) == 1
    f = bus.subscriber_failures[0]
    assert f.event == ev and isinstance(f.error, RuntimeError)


async def test_sink_failure_propagates() -> None:
    # The canonical store is the source of truth: if it can't persist, publish
    # must fail loudly rather than continue with a missing record.
    bus = EventBus(sink=ExplodingSink())
    reached: list[str] = []
    bus.subscribe(lambda e: reached.append("notified"))

    import pytest

    with pytest.raises(RuntimeError, match="disk is on fire"):
        await bus.publish(_event(uuid4()))
    assert reached == []  # no destination ran — we never got past the append


async def test_subscriber_failures_are_bounded() -> None:
    bus = EventBus(max_recorded_failures=3)

    def boom(ev: Event) -> None:
        raise RuntimeError("x")

    bus.subscribe(boom)
    for _ in range(10):
        await bus.publish(_event(uuid4()))
    assert len(bus.subscriber_failures) == 3  # deque maxlen, not 10


async def test_add_destination_ships_to_secondary_sink() -> None:
    primary = MemoryEventSink()
    secondary = MemoryEventSink()
    bus = EventBus(sink=primary)
    bus.add_destination(secondary)

    ev = _event(uuid4())
    await bus.publish(ev)

    assert await primary.count() == 1
    assert await secondary.count() == 1  # projected to the secondary destination


async def test_reads_delegate_to_canonical_store() -> None:
    bus = EventBus()  # in-memory canonical store
    task = uuid4()
    for _ in range(3):
        await bus.publish(_event(task))
    assert await bus.count({"task_id": task}) == 3
    assert len(await bus.query({"task_id": task})) == 3
    streamed = [e async for e in bus.stream({"task_id": task})]
    assert len(streamed) == 3


async def test_session_store_projection_is_compact() -> None:
    bus = EventBus()
    store = SessionStore()
    bus.subscribe(store)

    task = uuid4()
    await bus.publish(_event(task, event_types.TASK_STARTED))
    await bus.publish(_event(task, event_types.TURN_STARTED))
    await bus.publish(_event(task, event_types.TURN_STARTED))
    await bus.publish(_event(task, event_types.TASK_COMPLETED))

    assert store.event_count(task) == 4
    assert store.turns(task) == 2
    last = store.last(task)
    assert last is not None and last.type == event_types.TASK_COMPLETED
    assert store.tasks() == [task]

    store.evict(task)
    assert store.tasks() == []
    assert store.event_count(task) == 0  # gone; full history lives in the store


async def test_session_store_evict_on_terminal() -> None:
    store = SessionStore(evict_on_terminal=True)
    task = uuid4()
    store(_event(task, event_types.TASK_STARTED))
    assert store.tasks() == [task]
    store(_event(task, event_types.TASK_COMPLETED))
    assert store.tasks() == []  # auto-evicted on the terminal event
