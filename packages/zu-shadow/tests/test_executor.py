"""The live executor — replay a recorded path, GENERALISE it, and stop at the commit
boundary. Offline ($0): a fake browser scripts the live affordances, a ScriptedProvider
stands in for the model that fills variable choices.
"""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_providers.scripted import ScriptedProvider
from zu_shadow.capture import SemanticTarget
from zu_shadow.executor import Step, execute, steps_from_recording
from zu_shadow.recorder import RawInput, Recorder


class FakeSession:
    """Scripts one SurfaceView per perceive() and records each act — the live browser
    stand-in. The executor never sees a selector; it acts by opaque handle."""

    def __init__(self, surfaces: list[SurfaceView]) -> None:
        self._surfaces = list(surfaces)
        self.acts: list[tuple[str, str, str | None]] = []

    def perceive(self) -> SurfaceView:
        return self._surfaces.pop(0) if self._surfaces else SurfaceView()

    def act(self, handle: str, kind: str, value: str | None = None) -> None:
        self.acts.append((handle, kind, value))

    def current_url(self) -> str:
        return ""


def _surface(*affs: tuple[str, str, str]) -> SurfaceView:
    return SurfaceView(affordances=tuple(
        SurfaceAffordance(handle=h, role=r, label=lbl) for h, r, lbl in affs))


async def test_replays_the_demonstrated_path_exactly() -> None:
    steps = [
        Step(kind="type", role="searchbox", name="Search", value="muzzle"),
        Step(kind="click", role="button", name="Add to cart"),
        Step(kind="click", role="button", name="Check out"),
    ]
    session = FakeSession([
        _surface(("a1", "searchbox", "Search")),
        _surface(("a1", "button", "Add to cart")),
        _surface(("a1", "button", "Check out")),
    ])
    report = await execute(steps, session, ScriptedProvider.from_moves([]))
    assert report.completed
    assert [o.via for o in report.outcomes] == ["exact", "exact", "exact"]
    assert session.acts == [("a1", "type", "muzzle"), ("a1", "click", None), ("a1", "click", None)]


async def test_generalises_to_a_new_query_and_a_new_product() -> None:
    # Recorded a MUZZLE purchase; re-run it for COLLARS. The search value is overridden, and
    # the demonstrated muzzle link is gone — so the model picks a COLLAR from the live results.
    steps = [
        Step(kind="type", role="searchbox", name="Search", value="muzzle"),
        Step(kind="click", role="link", name="Wire Basket Muzzle with Quick Release Clip",
             intent="choosing a relevant product from the results"),
        Step(kind="click", role="button", name="Add to cart"),
    ]
    session = FakeSession([
        _surface(("a1", "searchbox", "Search")),                       # type the query
        _surface(("a1", "link", "Leather Dog Collar"),                 # results — NO muzzle here
                 ("a2", "link", "Padded Training Collar")),
        _surface(("a1", "button", "Add to cart")),                     # product page
    ])
    model = ScriptedProvider.from_moves([{"text": "a2", "finish": "stop"}])  # pick the collar
    report = await execute(steps, session, model, overrides={"search": "collars"})

    assert report.completed
    assert [o.via for o in report.outcomes] == ["exact", "model", "exact"]
    # generalised the PARAMETER: searched "collars", not "muzzle"
    assert session.acts[0] == ("a1", "type", "collars")
    # generalised the CHOICE: the model picked a collar product (a2), not the demonstrated muzzle
    assert session.acts[1] == ("a2", "click", None)
    assert session.acts[2] == ("a1", "click", None)


async def test_stops_at_the_commit_boundary() -> None:
    steps = [
        Step(kind="click", role="button", name="Check out"),
        Step(kind="click", role="button", name="Place order", committing=True),  # the charge
    ]
    session = FakeSession([_surface(("a1", "button", "Check out")),
                           _surface(("a1", "button", "Place order"))])
    report = await execute(steps, session, ScriptedProvider.from_moves([]))
    assert not report.completed and report.escalated_at == 1
    # the commit step was NEVER acted on (a real payment is brokered / routed to a human)
    assert session.acts == [("a1", "click", None)]
    assert report.outcomes[-1].via == "escalated"


async def test_escalates_when_no_target_resolves() -> None:
    steps = [Step(kind="click", role="button", name="Add to cart")]
    session = FakeSession([_surface(("a1", "link", "Home"), ("a2", "link", "About"))])
    model = ScriptedProvider.from_moves([{"text": "nope-not-a-handle", "finish": "stop"}])
    report = await execute(steps, session, model)
    assert not report.completed and report.escalated_at == 0
    assert session.acts == []  # never acted on a guessed/invalid handle


async def test_steps_from_recording_cleans_and_marks_commit() -> None:
    bus = EventBus()
    rec = Recorder(bus, site="https://shop.example")
    stream = [
        RawInput(kind="click", target=SemanticTarget(role="searchbox", name="Search", label="Search")),
        RawInput(kind="type", target=SemanticTarget(role="searchbox", name="Search", label="Search"),
                 value="muzzle"),  # focus-click + type on the same target -> collapses to the type
        RawInput(kind="type", target=SemanticTarget(role="textbox", name="Card number", label="Card number"),
                 value="4242424242424242"),  # blanked by redaction -> the agent never holds the card
        RawInput(kind="click", target=SemanticTarget(role="button", name="Place order", label="Place order")),
    ]
    session = await rec.record_stream(stream)
    steps = steps_from_recording(session.events)
    kinds = [(s.kind, s.name) for s in steps]
    assert ("click", "Search") not in kinds and ("type", "Search") in kinds  # focus-click dropped
    place = next(s for s in steps if s.name == "Place order")
    assert place.committing  # the irreversible order click is a commit boundary
    card = next(s for s in steps if s.name == "Card number")
    assert card.committing and card.value == "[REDACTED]"  # a payment field is brokered, never typed
    await bus.aclose()
