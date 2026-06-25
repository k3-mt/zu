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
