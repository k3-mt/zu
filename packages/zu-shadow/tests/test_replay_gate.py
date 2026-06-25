"""The verification-replay promotion gate: reproduces → promote; diverges → BLOCK."""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.contracts import Result, Status
from zu_providers.scripted import ScriptedProvider
from zu_shadow.recorder import RawInput, Recorder
from zu_shadow.replay_gate import verify_and_gate
from zu_shadow.synthesizer import Synthesizer


async def _synthesis_and_session(outcome: str):
    bus = EventBus()
    rec = Recorder(bus, site="https://x.example.com")
    session = await rec.record_stream(
        [RawInput(kind="navigate", url="https://x.example.com/go", intent="why")],
        outcome=outcome,
    )
    provider = ScriptedProvider.from_moves([{"text": '{"goal": "done"}', "finish": "stop"}])
    synthesis = await Synthesizer(provider).synthesize(session, "do the thing")
    await bus.aclose()
    return synthesis, session


async def test_promotes_when_replay_reproduces_recorded_outcome() -> None:
    synthesis, session = await _synthesis_and_session("3 slots found")

    async def runner(spec, cfg, bundle):
        # The synthesized agent reproduces the recorded outcome.
        return Result(status=Status.SUCCESS, value={"summary": "we found 3 slots found here"}), []

    verdict = await verify_and_gate(
        synthesis, session, spec=None, cfg=None, bundle=None, runner=runner
    )
    assert verdict.promote is True
    assert verdict.reproduced is True
    # The "why" intents ride along for review but are not part of the decision.
    assert verdict.intents_for_review


async def test_blocks_when_replay_diverges_from_recorded_outcome() -> None:
    synthesis, session = await _synthesis_and_session("3 slots found")

    async def runner(spec, cfg, bundle):
        # Succeeds, but the value does NOT reproduce the recorded outcome.
        return Result(status=Status.SUCCESS, value={"summary": "no slots available"}), []

    verdict = await verify_and_gate(
        synthesis, session, spec=None, cfg=None, bundle=None, runner=runner
    )
    assert verdict.promote is False
    assert verdict.reproduced is False
    assert "did not reproduce" in verdict.reason


async def test_blocks_when_replay_does_not_succeed() -> None:
    synthesis, session = await _synthesis_and_session("3 slots found")

    async def runner(spec, cfg, bundle):
        return Result(status=Status.TERMINAL, reason="loop exhausted"), []

    verdict = await verify_and_gate(
        synthesis, session, spec=None, cfg=None, bundle=None, runner=runner
    )
    assert verdict.promote is False
    assert "did not succeed" in verdict.reason


async def test_replay_crash_is_a_block_not_a_traceback() -> None:
    synthesis, session = await _synthesis_and_session("ok")

    async def runner(spec, cfg, bundle):
        raise RuntimeError("boom")

    verdict = await verify_and_gate(
        synthesis, session, spec=None, cfg=None, bundle=None, runner=runner
    )
    assert verdict.promote is False
    assert "boom" in verdict.reason
