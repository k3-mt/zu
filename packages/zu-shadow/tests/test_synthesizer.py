"""The synthesizer as a Zu agent (ScriptedProvider) — spec + FSM + invariants + egress."""

from __future__ import annotations

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.invariants import InvariantKind, PredicateKind, compile_spec
from zu_core.reachability import check_reachability
from zu_providers.scripted import ScriptedProvider
from zu_shadow.capture import SemanticTarget
from zu_shadow.recorder import RawInput, Recorder
from zu_shadow.synthesizer import Synthesizer


def _stream() -> list[RawInput]:
    return [
        RawInput(kind="navigate", url="https://vets.example.com/book"),
        RawInput(kind="network", url="https://api.vets.example.com/slots", status=200,
                 host="api.vets.example.com"),
        RawInput(kind="click",
                 target=SemanticTarget(role="button", name="Chislehurst", label="Location"),
                 intent="pick the right clinic"),
        RawInput(kind="network", url="https://cdn.vets.example.com/app.js", status=200,
                 host="cdn.vets.example.com"),
        RawInput(kind="click",
                 target=SemanticTarget(role="button", name="Next", label="Next")),
    ]


async def _synthesize():
    bus = EventBus()
    rec = Recorder(bus, site="https://vets.example.com")
    session = await rec.record_stream(_stream(), outcome="slots found")
    # The model writes the policy prompt + goal; offline a scripted reply suffices.
    provider = ScriptedProvider.from_moves(
        [{"text": '{"policy_prompt": "Find available vet slots", "goal": "slots"}',
          "finish": "stop"}]
    )
    result = await Synthesizer(provider).synthesize(session, "find vet appointment slots")
    await bus.aclose()
    return result


async def test_egress_allowlist_writes_itself_from_recorded_hosts() -> None:
    result = await _synthesize()
    # Exactly the hosts the recording touched — derived, not invented.
    assert result.egress == ["api.vets.example.com", "cdn.vets.example.com", "vets.example.com"]
    assert result.spec["capability_envelope"]["egress"] == result.egress


async def test_emits_core_fsm_reachable_to_goal() -> None:
    result = await _synthesize()
    verdict = check_reachability(result.fsm)
    assert verdict.reachable_goal  # the induced plan can reach the goal
    assert not verdict.traps
    assert result.fsm.initial == "start"
    assert "goal" in result.fsm.accepting


async def test_emits_core_invariants_compilable_to_monitors() -> None:
    result = await _synthesize()
    kinds = {i.predicate.kind for i in result.invariants}
    assert PredicateKind.DOMAIN_ALLOWLIST in kinds  # the egress rail
    assert any(i.kind is InvariantKind.EVENTUALLY for i in result.invariants)  # success criterion
    # They are real core Invariants → compile to Monitors with zero extra wiring.
    monitors = compile_spec(result.invariants)
    assert len(monitors) == len(result.invariants)
    # The egress allowlist invariant carries exactly the induced hosts.
    allow_inv = next(i for i in result.invariants
                     if i.predicate.kind is PredicateKind.DOMAIN_ALLOWLIST)
    assert set(allow_inv.predicate.params["allow"]) == set(result.egress)
    assert allow_inv.predicate.params["event_type"] == ev.SHADOW_NETWORK_RESPONSE


async def test_why_intents_surfaced_for_review_not_promoted() -> None:
    result = await _synthesize()
    assert result.intents_for_review  # the one click carried a "why"
    # The intent text is NOT silently baked into the policy prompt (it is reviewed).
    assert "pick the right clinic" not in result.spec["task"]["query"]


async def test_spec_unlocks_browser_tier_when_actions_present() -> None:
    result = await _synthesize()
    assert 2 in result.spec["tiers"]  # clicks/types → the browser tier is offered
    assert result.spec["task"]["max_tier"] == 2


def test_cleanup_collapses_widget_noise_and_strips_prices() -> None:
    # The induced FSM reads as clean GENERALISED steps, not instance soup.
    from zu_shadow.synthesizer import _clean_name, _clean_step_labels
    assert _clean_name("Add to cart £46.00") == "Add to cart"
    assert _clean_name("Wire Basket Muzzle with Quick Release Clip £46.00 GBP") == \
        "Wire Basket Muzzle with Quick Release Clip"
    assert _clean_name("Small - £46.00 GBP  Medium - £46.00 GBP") == "Small"  # option-dump → first
    # R2 drops a focus-click before a type on the same target; R1 collapses a doubled step.
    labels = ["click:Search", "type:Search", "click:Large", "click:Large", "type:Large",
              "click:Card number", "type:Card number"]
    assert _clean_step_labels(labels) == [
        "type:Search", "click:Large", "type:Large", "type:Card number"]


async def test_a_recording_feeds_the_section5_pathfinder() -> None:
    # The synthesizer's induced FSM IS a §5 pathfinder transition model: the guided search
    # plans a path over it, and a SECOND recording merges/grows it (the apprenticeship
    # premise). Leaf import of zu_patterns — no production dependency from zu-shadow.
    import pytest
    search = pytest.importorskip("zu_patterns.search")
    from zu_core.reachability import co_reachable, trap_states

    result = await _synthesize()
    model = search.fsm_from_shadow(result.fsm)              # Shadow recording → transition model
    assert len(trap_states(model)) == 0
    assert len(co_reachable(model)) == len(model.states)    # every state can still reach the goal
    plan = search.plan(model)                               # the §5 guided search plans over it
    assert plan.reached_goal and len(plan.steps) >= 1
    grown = search.fsm_from_shadow(result.fsm, base=model)  # a 2nd recording merges in (grows it)
    assert len(grown.states) >= len(model.states)
