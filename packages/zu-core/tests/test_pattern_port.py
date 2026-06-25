"""The Pattern port — a hand-written dummy satisfies the runtime_checkable shape,
the registry knows the kind, and the SURFACE_CONTAINS predicate folds surface
events. All offline, $0."""

from __future__ import annotations

from types import SimpleNamespace

from zu_core import events as ev
from zu_core.invariants import (
    Invariant,
    InvariantKind,
    Predicate,
    PredicateKind,
    compile_invariant,
    predicate_holds,
)
from zu_core.ports import INTERFACE_VERSION, Pattern, PatternStep, RecognitionResult
from zu_core.registry import GROUPS, Registry
from zu_core.surface import SurfaceAffordance, SurfaceView


class _DummyPattern:
    name = "dummy"
    archetype = "dummy_archetype"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        if any(a.role == "button" for a in surface.affordances):
            return RecognitionResult(
                archetype=self.archetype,
                confidence=0.9,
                matched_handles=("a1",),
                script=(PatternStep(op="click", role="button"),),
            )
        return None

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        return []

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        return []


def test_dummy_satisfies_pattern_protocol() -> None:
    assert isinstance(_DummyPattern(), Pattern)


def test_patterns_interface_version_present() -> None:
    assert INTERFACE_VERSION["patterns"] == 1


def test_patterns_group_registered() -> None:
    assert GROUPS["patterns"] == "zu.patterns"
    reg = Registry()
    reg.register("patterns", "dummy", _DummyPattern())
    assert "dummy" in reg.names("patterns")


def _ev(etype: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=etype, payload=payload)


def test_surface_contains_handle_present_and_absent() -> None:
    present = Predicate(
        kind=PredicateKind.SURFACE_CONTAINS,
        params={"event_type": ev.SURFACE_CAPTURED, "handle": "a7"},
    )
    events = [_ev(ev.SURFACE_CAPTURED, {"handles": ["a1", "a7"]})]
    assert predicate_holds(present, events) is True
    events_absent = [_ev(ev.SURFACE_CAPTURED, {"handles": ["a1", "a2"]})]
    assert predicate_holds(present, events_absent) is False


def test_surface_contains_negate_holds_when_absent() -> None:
    gone = Predicate(
        kind=PredicateKind.SURFACE_CONTAINS,
        params={"event_type": ev.SURFACE_CAPTURED, "handle": "a1", "negate": True},
    )
    # banner button still present ⇒ "it is gone" is FALSE
    assert predicate_holds(gone, [_ev(ev.SURFACE_CAPTURED, {"handles": ["a1"]})]) is False
    # banner button absent ⇒ "it is gone" is TRUE
    assert predicate_holds(gone, [_ev(ev.SURFACE_CAPTURED, {"handles": ["a2"]})]) is True


def test_surface_contains_archetype_from_recognition_event() -> None:
    p = Predicate(
        kind=PredicateKind.SURFACE_CONTAINS,
        params={"event_type": ev.PATTERN_RECOGNIZED, "archetype": "login_form"},
    )
    assert predicate_holds(p, [_ev(ev.PATTERN_RECOGNIZED, {"archetype": "login_form"})]) is True
    assert predicate_holds(p, [_ev(ev.PATTERN_RECOGNIZED, {"archetype": "search_box"})]) is False


def test_surface_contains_no_matching_event_is_inert_true() -> None:
    p = Predicate(
        kind=PredicateKind.SURFACE_CONTAINS,
        params={"event_type": ev.SURFACE_CAPTURED, "handle": "a1"},
    )
    assert predicate_holds(p, []) is True  # absence of evidence is not a violation


def test_compiled_surface_invariant_violates_on_absence() -> None:
    inv = Invariant(
        name="login.reached_account",
        kind=InvariantKind.THROUGHOUT,
        predicate=Predicate(
            kind=PredicateKind.SURFACE_CONTAINS,
            params={"event_type": ev.SURFACE_CAPTURED, "label": "Logout"},
        ),
    )
    mon = compile_invariant(inv)
    # a surface that does NOT show the expected post-state ⇒ VIOLATION
    ctx = SimpleNamespace(
        events=[_ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "Sign in"}]})]
    )
    verdict = mon.evaluate(ctx)  # type: ignore[arg-type]
    assert verdict is not None and verdict.state.value == "violation"
    # the expected post-state present ⇒ inert
    ctx_ok = SimpleNamespace(
        events=[_ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "Logout"}]})]
    )
    assert mon.evaluate(ctx_ok) is None  # type: ignore[arg-type]


def test_pattern_recognized_is_a_data_event() -> None:
    assert ev.PATTERN_RECOGNIZED in ev.DATA_TYPES
    assert ev.PATTERN_RECOGNIZED in ev.ALL_TYPES
    assert ev.PATTERN_RECOGNIZED.startswith("data.")


_USED = (SurfaceAffordance,)
