"""The live executor — replay a recorded path, GENERALISE it, and stop at the commit
boundary. Offline ($0): a fake browser scripts the live affordances, a ScriptedProvider
stands in for the model that fills variable choices.
"""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.content_view import ContentView, Want
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_providers.scripted import ScriptedProvider
from zu_shadow.capture import SemanticTarget
from zu_shadow.executor import Step, execute, steps_from_recording
from zu_shadow.recorder import RawInput, Recorder


class FakeSession:
    """The live browser stand-in. Holds a list of page surfaces; ``perceive`` returns the
    CURRENT one and ``act`` records and advances to the next page (a click navigates). With
    ``advance_on_perceive=True`` the page instead settles between perceives (models lazy
    content loading). The executor never sees a selector; it acts by opaque handle."""

    def __init__(self, surfaces: list[SurfaceView], *, advance_on_perceive: bool = False) -> None:
        self._s = list(surfaces)
        self._i = 0
        self._pa = advance_on_perceive
        self.acts: list[tuple[str, str, str | None]] = []

    def perceive(self) -> SurfaceView:
        s = self._s[min(self._i, len(self._s) - 1)] if self._s else SurfaceView()
        if self._pa:
            self._i += 1
        return s

    def act(self, handle: str, kind: str, value: str | None = None) -> None:
        self.acts.append((handle, kind, value))
        if not self._pa:
            self._i += 1

    def current_url(self) -> str:
        return ""

    def content_view(self, want: frozenset[Want]) -> ContentView:
        # The content-free path tests never escalate, so the second projection is
        # never read; satisfy the BrowserSession Protocol with an empty view.
        return ContentView()


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


async def test_dismisses_a_cookie_banner_and_continues() -> None:
    # A consent banner that wasn't in the recording blocks the step → the executor dismisses
    # it and re-perceives instead of giving up.
    steps = [Step(kind="click", role="button", name="Add to cart")]
    session = FakeSession([
        _surface(("a1", "button", "Accept all cookies")),  # the banner is all that's actionable
        _surface(("a1", "button", "Add to cart")),          # ...the page, once it's dismissed
    ])
    report = await execute(steps, session, ScriptedProvider.from_moves([]))
    assert report.completed
    assert session.acts == [("a1", "click", None), ("a1", "click", None)]  # dismiss, then add to cart
    assert any(o.via == "interstitial" for o in report.outcomes)


async def test_retries_perceive_for_lazy_loaded_content() -> None:
    # The target isn't rendered on the first perceive (no banner, just loading) → retry, and
    # find it WITHOUT falling back to a (possibly wrong) model choice.
    steps = [Step(kind="click", role="button", name="Add to cart")]
    session = FakeSession([
        _surface(("a1", "link", "Home")),                   # still loading — no Add to cart yet
        _surface(("a1", "button", "Add to cart")),          # ...appears on the next perceive
    ], advance_on_perceive=True)
    report = await execute(steps, session, ScriptedProvider.from_moves([]))
    assert report.completed and [o.via for o in report.outcomes] == ["exact"]
    assert session.acts == [("a1", "click", None)]          # the model was never asked


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


async def test_commit_boundary_detected_structurally_on_non_english_button() -> None:
    # A commit control identified STRUCTURALLY (a form-submit) is a commit boundary
    # regardless of label language: a German "Bezahlen" submit button yields committing
    # even though it matches no English _COMMIT phrase.
    bus = EventBus()
    rec = Recorder(bus, site="https://shop.example.de")
    stream = [
        RawInput(kind="click", target=SemanticTarget(
            role="button", name="Bezahlen", label="Bezahlen", submits=True)),  # "Pay" in German
    ]
    session = await rec.record_stream(stream)
    steps = steps_from_recording(session.events)
    pay = next(s for s in steps if s.name == "Bezahlen")
    assert pay.committing  # structural submit signal → commit boundary, not an English phrase
    await bus.aclose()

    # And the executor ESCALATES at it (never auto-crosses the payment boundary).
    session2 = FakeSession([_surface(("a1", "button", "Bezahlen"))])
    report = await execute(steps, session2, ScriptedProvider.from_moves([]))
    assert not report.completed and report.escalated_at == 0
    assert session2.acts == []  # the commit was never clicked
    assert report.outcomes[-1].via == "escalated"


async def test_regression_english_only_would_auto_cross_non_english_pay_button() -> None:
    # The pre-fix English-only path: a non-English pay button with NO structural signal
    # threaded through is NOT recognised as a commit — proving the old code auto-crossed.
    bus = EventBus()
    rec = Recorder(bus, site="https://shop.example.de")
    stream = [RawInput(kind="click", target=SemanticTarget(
        role="button", name="Bezahlen", label="Bezahlen"))]  # no submits signal → fallback only
    session = await rec.record_stream(stream)
    steps = steps_from_recording(session.events)
    pay = next(s for s in steps if s.name == "Bezahlen")
    assert not pay.committing  # the English fallback silently misses — the documented gap
    # With the structural signal set (the fix), the same button IS a commit boundary.
    bus2 = EventBus()
    rec2 = Recorder(bus2, site="https://shop.example.de")
    session2 = await rec2.record_stream([RawInput(kind="click", target=SemanticTarget(
        role="button", name="Bezahlen", label="Bezahlen", submits=True))])
    steps2 = steps_from_recording(session2.events)
    assert next(s for s in steps2 if s.name == "Bezahlen").committing
    await bus.aclose()
    await bus2.aclose()


async def test_cc_autocomplete_field_is_a_commit_boundary_regardless_of_label() -> None:
    # A payment-card field carrying autocomplete=cc-csc (a CVV) is a brokered commit
    # boundary even when its label is non-English ("Prüfziffer") and non-Luhn.
    bus = EventBus()
    rec = Recorder(bus, site="https://shop.example.de")
    stream = [RawInput(kind="type", value="123", target=SemanticTarget(
        role="textbox", name="Prüfziffer", label="Prüfziffer", autocomplete="cc-csc"))]
    session = await rec.record_stream(stream)
    steps = steps_from_recording(session.events)
    cvv = next(s for s in steps if s.name == "Prüfziffer")
    assert cvv.committing  # cc-* autocomplete → payment-card control, brokered (§8)
    # And its value was blanked (credential-marked structurally), not recorded verbatim.
    assert cvv.value == "[REDACTED]"
    await bus.aclose()


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
