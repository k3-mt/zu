"""The recorder over a synthetic abstract input/CDP stream — semantic capture."""

from __future__ import annotations

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_shadow.capture import SemanticTarget
from zu_shadow.recorder import RawInput, Recorder


def _stream() -> list[RawInput]:
    return [
        RawInput(kind="navigate", url="https://vets.example.com/book", intent="open booking"),
        RawInput(kind="page", url="https://vets.example.com/book", title="Book"),
        RawInput(kind="click",
                 target=SemanticTarget(role="button", name="Chislehurst", label="Location"),
                 intent="pick the right clinic"),
        RawInput(kind="network", url="https://api.vets.example.com/slots", status=200,
                 host="api.vets.example.com"),
        RawInput(kind="type",
                 target=SemanticTarget(role="textbox", name="Name", label="Your name"),
                 value="Alex"),
    ]


async def test_records_semantic_shadow_events() -> None:
    bus = EventBus()
    rec = Recorder(bus, site="https://vets.example.com")
    session = await rec.record_stream(_stream(), outcome="3 slots found")

    types = [e.type for e in session.shadow_events()]
    assert types[0] == ev.SHADOW_SESSION_START
    assert types[-1] == ev.SHADOW_SESSION_END
    assert ev.SHADOW_USER_CLICK in types
    assert ev.SHADOW_USER_TYPE in types
    assert ev.SHADOW_USER_NAVIGATE in types
    assert ev.SHADOW_NETWORK_RESPONSE in types

    # SEMANTIC capture: the click target is {role,name,label} — no selector/coords.
    click = next(e for e in session.events if e.type == ev.SHADOW_USER_CLICK)
    assert set(click.payload["target"]) == {"role", "name", "label"}
    assert click.payload["target"]["name"] == "Chislehurst"
    assert "selector" not in click.payload and "x" not in click.payload

    end = next(e for e in session.events if e.type == ev.SHADOW_SESSION_END)
    assert end.payload["steps"] == 3  # navigate + click + type, not page/network
    await bus.aclose()


async def test_scroll_is_recorded_as_context_not_an_action_step() -> None:
    bus = EventBus()
    rec = Recorder(bus, site="https://vets.example.com")
    stream = [
        RawInput(kind="navigate", url="https://vets.example.com/book"),
        RawInput(kind="scroll", value="down", status=1400),   # had to scroll to find it
        RawInput(kind="click",
                 target=SemanticTarget(role="button", name="Book", label="Book")),
    ]
    session = await rec.record_stream(stream, outcome="done")
    types = [e.type for e in session.shadow_events()]
    assert ev.SHADOW_USER_SCROLL in types
    scroll = next(e for e in session.events if e.type == ev.SHADOW_USER_SCROLL)
    assert scroll.payload["direction"] == "down" and scroll.payload["y"] == 1400
    end = next(e for e in session.events if e.type == ev.SHADOW_SESSION_END)
    assert end.payload["steps"] == 2  # navigate + click; the scroll is context, not a step
    await bus.aclose()


async def test_all_shadow_events_are_namespaced_data() -> None:
    bus = EventBus()
    rec = Recorder(bus, site="s")
    session = await rec.record_stream(_stream())
    for e in session.shadow_events():
        assert e.type.startswith("data.shadow.")
    await bus.aclose()
