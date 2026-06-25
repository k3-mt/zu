"""The apprenticeship loop (§3.4): a human rescue becomes a Shadow demonstration.

Proves, offline and deterministic ($0):

  * a synthetic resolved escalation (a :class:`PausedRun`) folds into a REDACTED
    zu-shadow ``RecordedSession`` carrying the operator's "why" intent — the same
    shape the synthesizer/induction already consume;
  * the operator's "why" and any secret in the rescued invocation are REDACTED
    before they reach the demonstration log (Shadow discipline, reused);
  * a rescue-derived agent is REVIEW-GATED: ``verify_and_gate`` BLOCKS promotion of
    an agent whose replay does not reproduce the recorded outcome — never
    auto-promoted — and PROMOTES only one that reproduces it.
"""

from __future__ import annotations

from uuid import uuid4

from zu_cli.apprentice import demonstration_from_rescue, record_rescue, synthesize_rescue
from zu_cli.handoff import PausedRun
from zu_core.contracts import Result, Status, TaskSpec
from zu_providers.scripted import ScriptedProvider


def _rescue(*, with_secret: bool = True) -> PausedRun:
    """A synthetic resolved captcha rescue — the shape the handoff queue holds."""
    url = "https://site/login?token=SECRET123" if with_secret else "https://site/login"
    return PausedRun(
        run_id=str(uuid4()),
        spec=TaskSpec(query="log in", target="site"),
        provider=None, registry=None, bus=None, providers={}, run_kwargs={},
        events=[],
        approval_id="ap-1",
        pending={"tool": "open_login", "args": {"url": url}, "idempotency_key": "k1"},
        reason="captcha",
        detail="captcha wall",
    )


async def test_rescue_folds_into_a_redacted_shadow_recording():
    run = _rescue()
    session = await record_rescue(run, why="I completed the captcha; token ?token=SECRET123", by="alice")
    # It IS a Shadow recording: data.shadow.* events on the log.
    shadow = session.shadow_events()
    assert shadow, "the rescue produced no shadow events"
    types = [e.type for e in shadow]
    assert any(t.endswith("user.navigate") for t in types)
    assert any(t.endswith("user.click") for t in types)

    # The operator's "why" rides as the action intent — and is REDACTED (the token
    # in it is gone, swept at capture before append).
    blob = "".join(str(e.payload) for e in shadow)
    assert "SECRET123" not in blob
    intents = [e.payload.get("intent") for e in shadow if e.payload.get("intent")]
    assert intents and all("SECRET123" not in (i or "") for i in intents)
    # The recorded outcome names the human resolution — the curriculum label.
    assert "resolved the escalation" in (session.outcome or "")


def test_demonstration_record_is_review_gated_never_promoted():
    rec = demonstration_from_rescue(_rescue(), why="solved it; key sk-ABCDEFGHIJKLMNOP", by="alice")
    assert rec["promoted"] is False  # NEVER auto-promoted
    assert rec["status"] == "recorded-for-review"
    assert rec["reason"] == "captcha"
    assert "sk-ABCDEFGHIJKLMNOP" not in str(rec["why"])  # redacted operator intent


async def test_synthesize_rescue_proposes_an_agent_for_review():
    # The synthesizer turns the rescue recording into a PROPOSAL (spec + induced
    # FSM/invariants). It is a proposal only — promotion is gated downstream.
    provider = ScriptedProvider.from_moves(
        [{"text": '{"policy_prompt": "log in like the human did", "goal": "logged in"}',
          "finish": "stop"}]
    )
    synthesis, session = await synthesize_rescue(_rescue(), provider, why="completed the wall")
    assert synthesis.spec["task"]["query"]  # a policy prompt was proposed
    assert session.shadow_events()  # backed by the recorded demonstration


async def test_unverified_rescue_agent_is_blocked_from_promotion():
    # The review gate (reused from Shadow) BLOCKS a rescue-derived agent whose replay
    # does not reproduce the recorded outcome — it never becomes agent behavior.
    from zu_shadow.replay_gate import verify_and_gate

    provider = ScriptedProvider.from_moves(
        [{"text": '{"policy_prompt": "p", "goal": "logged in"}', "finish": "stop"}]
    )
    synthesis, session = await synthesize_rescue(_rescue(), provider, why="did it")

    async def diverging_runner(spec, cfg, bundle):  # noqa: ANN001, ANN202
        # The replayed agent "succeeds" but produces a value that does NOT reproduce
        # the recorded outcome ("...resolved the escalation...").
        return Result(status=Status.SUCCESS, value={"answer": "something unrelated"}), []

    verdict = await verify_and_gate(
        synthesis, session, spec=None, cfg=None, bundle=None, runner=diverging_runner,
    )
    assert verdict.promote is False  # BLOCKED — held, not promoted
    assert "did not reproduce" in verdict.reason


async def test_reproducing_rescue_agent_is_eligible_for_promotion():
    # The PASS path: a replay that reproduces the recorded outcome clears the gate.
    from zu_shadow.replay_gate import verify_and_gate

    provider = ScriptedProvider.from_moves(
        [{"text": '{"policy_prompt": "p", "goal": "g"}', "finish": "stop"}]
    )
    synthesis, session = await synthesize_rescue(_rescue(), provider, why="did it")
    recorded = session.outcome  # the human-stated outcome the agent must reproduce

    async def reproducing_runner(spec, cfg, bundle):  # noqa: ANN001, ANN202
        return Result(status=Status.SUCCESS, value={"summary": recorded}), []

    verdict = await verify_and_gate(
        synthesis, session, spec=None, cfg=None, bundle=None, runner=reproducing_runner,
    )
    assert verdict.promote is True
    assert verdict.reproduced is True
