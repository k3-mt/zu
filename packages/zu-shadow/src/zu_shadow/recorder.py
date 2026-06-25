"""The recorder — a human session, folded onto the event log.

A Shadow recording IS the event bus run over a HUMAN session. The recorder
consumes an ABSTRACT input/CDP event stream (a sequence of ``RawInput`` items —
clicks, types, navigations, page loads, network responses) and emits the
``data.shadow.*`` events for them. The stream is abstract on purpose: a SYNTHETIC
stream drives the whole recorder offline at $0 (fully tested), while the LIVE CDP
binding (real Chromium + a real human) sits behind a manual entrypoint
(``live.py``) and produces the SAME ``RawInput`` items — so the offline core is
exercised exactly as the live path is.

**Redaction precedes append.** Every event passes through
:func:`zu_shadow.redaction.redact_event` BEFORE :meth:`EventBus.publish` (which is
the only thing that calls :meth:`EventSink.append`). The recorder owns the sole
path an event takes to the bus, so there is no way for an un-redacted secret to
reach the append-only log: the secret is gone before the event is hashed into the
chain. This is the ZU-AUDIT-4 guarantee, proved by a named offline test.

The "why" intent affordance is the human's optional one-sentence narration of a
step; it rides on the action event's ``intent`` field, is redacted like everything
else, and is REVIEWED — never auto-promoted — by the synthesizer.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Event

from . import capture
from .capture import SemanticTarget
from .redaction import RedactionPolicy, redact_event


@dataclass(frozen=True)
class RawInput:
    """One item off the abstract input/CDP stream — the same shape the live CDP
    binding produces and a synthetic test stream produces. ``kind`` is one of
    ``click`` | ``type`` | ``navigate`` | ``page`` | ``network``; the rest are the
    fields that kind needs. ``intent`` is the human's optional "why" narration."""

    kind: str
    target: SemanticTarget | None = None
    value: str | None = None
    url: str = ""
    title: str = ""
    status: int = 200
    host: str = ""
    intent: str | None = None
    # A live CDP network response carries response headers (Set-Cookie, Authorization,
    # ...). They ride on the raw stream item but the recorder DELIBERATELY DROPS them:
    # a network event is metadata-only (url/status/host), so a Set-Cookie never enters
    # the log in the first place — the strongest form of "redacted before append."
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class RecordedSession:
    """The result of a recording: the (already redacted) events on the log, the
    site the session ran against, and an optional human-stated outcome. This is the
    input the synthesizer consumes (it never sees an un-redacted event)."""

    site: str
    events: list[Event] = field(default_factory=list)
    outcome: str | None = None

    def shadow_events(self) -> list[Event]:
        """Just the ``data.shadow.*`` events, in order."""
        return [e for e in self.events if e.type.startswith("data.shadow.")]


class Recorder:
    """Folds an abstract input/CDP stream into redacted ``data.shadow.*`` events on a
    bus. One recorder = one human session. Redaction is DEFAULT-ON; pass a custom
    :class:`RedactionPolicy` to add consumer PII patterns, never to weaken the floor.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        site: str,
        policy: RedactionPolicy | None = None,
        trace_id: UUID | None = None,
        task_id: UUID | None = None,
    ) -> None:
        self._bus = bus
        self._site = site
        self._policy = policy or RedactionPolicy()
        self._trace_id = trace_id or uuid4()
        self._task_id = task_id or uuid4()
        self._root_id: UUID | None = None
        self._steps = 0

    def _make(self, type_: str, payload: dict, *, parent: UUID | None) -> Event:
        return Event(
            trace_id=self._trace_id,
            task_id=self._task_id,
            parent_id=parent,
            type=type_,
            source="zu-shadow.recorder",
            payload=payload,
        )

    async def _emit(self, type_: str, payload: dict, *, parent: UUID | None) -> Event:
        """Build, REDACT, then publish. Redaction happens here — before
        ``bus.publish`` (which calls ``sink.append``) — so no un-redacted secret can
        reach the log. Returns the (redacted) event as it was published."""
        raw = self._make(type_, payload, parent=parent)
        redacted = redact_event(raw, self._policy)
        assert isinstance(redacted, Event)  # redact_event preserves the Event type
        await self._bus.publish(redacted)
        return redacted

    async def start(self) -> Event:
        """Open the recording — the session root every action is parented to."""
        root = await self._emit(
            ev.SHADOW_SESSION_START, {"site": self._site, "started_by": "human"}, parent=None
        )
        self._root_id = root.event_id
        return root

    async def record(self, item: RawInput) -> Event:
        """Fold one raw stream item into a redacted shadow event. Action items
        (click/type/navigate) count as steps; page/network are context."""
        if self._root_id is None:
            await self.start()
        type_, payload = self._to_event(item)
        if type_ in (ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE, ev.SHADOW_USER_NAVIGATE):
            self._steps += 1
        return await self._emit(type_, payload, parent=self._root_id)

    def _to_event(self, item: RawInput) -> tuple[str, dict]:
        if item.kind == "click":
            assert item.target is not None, "a click needs a semantic target"
            return capture.capture_click(item.target, intent=item.intent)
        if item.kind == "type":
            assert item.target is not None, "a type needs a semantic target"
            return capture.capture_type(item.target, item.value or "", intent=item.intent)
        if item.kind == "navigate":
            return capture.capture_navigate(item.url, intent=item.intent)
        if item.kind == "page":
            return capture.capture_page_loaded(item.url, item.title)
        if item.kind == "network":
            # item.headers is intentionally NOT forwarded — metadata-only capture drops
            # response headers (Set-Cookie/Authorization) at source, so they never log.
            return capture.capture_network_response(item.url, item.status, item.host)
        raise ValueError(f"unknown raw input kind {item.kind!r}")

    async def end(self, *, outcome: str | None = None) -> Event:
        """Close the recording. ``outcome`` is the human's stated result — redacted
        like everything else (it is free text)."""
        if self._root_id is None:
            await self.start()
        return await self._emit(
            ev.SHADOW_SESSION_END,
            {"outcome": outcome, "steps": self._steps},
            parent=self._root_id,
        )

    async def record_stream(
        self, stream: Iterable[RawInput] | Sequence[RawInput], *, outcome: str | None = None
    ) -> RecordedSession:
        """Drive a whole abstract stream end to end and return the recorded session.
        The convenience the synthetic-stream tests and the live binding both use."""
        await self.start()
        for item in stream:
            await self.record(item)
        await self.end(outcome=outcome)
        events = await self._bus.query()
        return RecordedSession(site=self._site, events=list(events), outcome=outcome)


def session_from_events(events: list[Any], *, site: str = "",
                        outcome: str | None = None) -> RecordedSession:
    """Reconstruct a :class:`RecordedSession` from an event log (e.g. one loaded from
    a durable sink). The site falls back to the ``session.start`` payload."""
    if not site:
        for e in events:
            if getattr(e, "type", "") == ev.SHADOW_SESSION_START:
                site = (getattr(e, "payload", {}) or {}).get("site", "")
                break
    return RecordedSession(site=site, events=list(events), outcome=outcome)
