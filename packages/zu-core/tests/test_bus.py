"""Build step 3 — the append-before-notify bus + the session-store projection.

Proves: the event is durably appended *before* any subscriber is notified;
every subscriber is told; and one crashing subscriber doesn't stop the rest
(its failure is recorded, not lost).
"""

from __future__ import annotations

from uuid import uuid4

from zu_core import events as event_types
from zu_core.bus import EventBus
from zu_core.contracts import Event
from zu_core.projections import SessionStore


class FakeSink:
    """An in-memory EventSink — keeps the core test independent of zu-backends."""

    def __init__(self) -> None:
        self.appended: list[Event] = []

    async def append(self, event: Event) -> None:
        self.appended.append(event)

    async def query(self, flt: dict | None = None) -> list[Event]:
        return list(self.appended)


def _event(task_id, type=event_types.TASK_STARTED) -> Event:
    return Event(trace_id=uuid4(), task_id=task_id, type=type, source="loop")


async def test_append_before_notify() -> None:
    sink = FakeSink()
    bus = EventBus(sink=sink)
    seen_in_sink_at_notify: list[list[Event]] = []

    def subscriber(ev: Event) -> None:
        # snapshot what the sink already holds at the moment we're notified
        seen_in_sink_at_notify.append(list(sink.appended))

    bus.subscribe(subscriber)
    ev = _event(uuid4())
    await bus.publish(ev)

    # the event was already durable in the sink before the subscriber ran
    assert seen_in_sink_at_notify == [[ev]]


async def test_every_subscriber_notified() -> None:
    bus = EventBus()
    got: list[str] = []
    bus.subscribe(lambda e: got.append("a"))
    bus.subscribe(lambda e: got.append("b"))
    bus.subscribe(lambda e: got.append("c"))
    await bus.publish(_event(uuid4()))
    assert sorted(got) == ["a", "b", "c"]


async def test_one_crashing_subscriber_doesnt_stop_the_rest() -> None:
    bus = EventBus()
    reached: list[str] = []

    def boom(ev: Event) -> None:
        raise RuntimeError("projection blew up")

    bus.subscribe(lambda e: reached.append("before"))
    bus.subscribe(boom)
    bus.subscribe(lambda e: reached.append("after"))

    ev = _event(uuid4())
    await bus.publish(ev)  # must not raise

    assert reached == ["before", "after"]  # the crash didn't stop the third one
    assert len(bus.subscriber_failures) == 1
    f = bus.subscriber_failures[0]
    assert f.event == ev and isinstance(f.error, RuntimeError)


async def test_async_subscriber_is_awaited() -> None:
    bus = EventBus()
    got: list[str] = []

    async def async_sub(ev: Event) -> None:
        got.append("async")

    bus.subscribe(async_sub)
    await bus.publish(_event(uuid4()))
    assert got == ["async"]


async def test_session_store_projection() -> None:
    bus = EventBus()
    store = SessionStore()
    bus.subscribe(store)

    task = uuid4()
    await bus.publish(_event(task, event_types.TASK_STARTED))
    await bus.publish(_event(task, event_types.TURN_STARTED))
    await bus.publish(_event(task, event_types.TURN_STARTED))
    await bus.publish(_event(task, event_types.TASK_COMPLETED))

    assert len(store.events(task)) == 4
    assert store.turns(task) == 2
    last = store.last(task)
    assert last is not None and last.type == event_types.TASK_COMPLETED
    assert store.tasks() == [task]
