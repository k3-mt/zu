"""Built-in triggers — the inbound mirror of the event sinks (§4.4).

A :class:`~zu_core.ports.Trigger` listens for events *in* and yields a
:class:`~zu_core.ports.TriggerEvent` to start a run, exactly as an EventSink
emits events *out*. Each built-in differs only in its ``source`` label and how
it obtains raw payloads; the payload it yields is **untrusted** by contract.

The transport (an HTTP server for webhooks, a broker client for a queue, a cron
clock for a schedule) is the *integration* and is injected as a plain
``Iterable``/callable of raw payloads — which keeps every trigger deterministic
and offline-testable: feed it payloads, assert the typed events it yields. A
real deployment feeds the same seam from the live transport.

``drive`` is the thin glue that turns a trigger into runs: iterate ``listen()``
and hand each event to an (async) handler — the runtime's "wake an agent on an
inbound event" in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator

from zu_core.ports import TriggerEvent


class _IterableTrigger:
    """Shared base: wrap an iterable of raw payload dicts as typed, source-tagged
    :class:`TriggerEvent`\\s. A non-dict payload is wrapped as ``{"value": …}`` so
    a malformed inbound message is still typed, never crashing the listener."""

    source = "event"

    def __init__(self, payloads: Iterable[object] | None = None) -> None:
        # Payloads are untrusted and arbitrary — a transport may hand us anything;
        # ``listen`` types each one (a non-dict is wrapped) so nothing crashes.
        self._payloads = payloads if payloads is not None else []

    def listen(self) -> Iterator[TriggerEvent]:
        for raw in self._payloads:
            payload = raw if isinstance(raw, dict) else {"value": raw}
            yield TriggerEvent(source=self.source, payload=payload)


class WebhookTrigger(_IterableTrigger):
    """Wake on an inbound webhook. The HTTP server pushes each request body into
    the injected iterable; the trigger drains it as untrusted payloads."""

    source = "webhook"


class QueueTrigger(_IterableTrigger):
    """Wake on a queue message (SQS / Kafka / PubSub). The broker consumer feeds
    messages into the injected iterable."""

    source = "queue"


class ObjectStoreTrigger(_IterableTrigger):
    """Wake on an object-storage write (an S3/GCS event notification)."""

    source = "object-store"


class ScheduleTrigger:
    """Wake on a schedule. Each tick from the injected ``ticks`` iterable yields
    one event ``{"tick": <tick>}`` — a real deployment feeds ticks from a cron
    clock; tests feed a fixed list, so the schedule is deterministic."""

    source = "schedule"

    def __init__(self, ticks: Iterable | None = None) -> None:
        self._ticks = ticks if ticks is not None else []

    def listen(self) -> Iterator[TriggerEvent]:
        for tick in self._ticks:
            yield TriggerEvent(source=self.source, payload={"tick": tick})


async def drive(
    trigger: object,
    handle: Callable[[TriggerEvent], Awaitable[object]],
    *,
    max_events: int | None = None,
) -> int:
    """Drive a trigger: for each event it yields, await ``handle(event)`` (start
    a run). Returns the number of events handled. ``max_events`` bounds the loop
    (a test, or a drain-N deployment); ``None`` runs until the trigger's
    iterator is exhausted."""
    handled = 0
    for event in trigger.listen():  # type: ignore[attr-defined]
        await handle(event)
        handled += 1
        if max_events is not None and handled >= max_events:
            break
    return handled
