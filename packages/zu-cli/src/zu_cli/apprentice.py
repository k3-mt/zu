"""The apprenticeship loop — a human rescue IS a demonstration (§3.4).

The novel, compounding piece: when an operator resolves an escalation, that
intervention is not just an unblock — it is a labelled demonstration at the exact
edge of the agent's competence (the escalation points are the curriculum). This
module turns a resolved :class:`zu_cli.handoff.PausedRun` into a zu-shadow
demonstration WITH the operator's "why" intent, so it feeds the SAME synthesizer
and induction every recorded session does.

Three rules are absolute (they mirror Shadow's own):

  * REDACTED. The demonstration is built through the Recorder, which runs every
    event through the default-on redaction stage BEFORE it touches the log — so a
    secret in the paused invocation's args (a token in a url) never reaches the
    demonstration log, and the operator's "why" is swept too.
  * REVIEW-GATED, NEVER AUTO-PROMOTED. This module only RECORDS the demonstration
    and (optionally) synthesizes a PROPOSAL. Promotion is decided downstream by
    ``zu_shadow.replay_gate.verify_and_gate`` — a rescue-derived agent does not
    become agent behavior until it reproduces the recorded outcome. ``record_*``
    never promotes anything.
  * AUTHORIZATION-SCOPED. The rescue is a demonstration of a step the operator was
    entitled to perform on a system the agent is entitled to operate; nothing here
    teaches the agent to defeat a defense (a captcha rescue records "a human
    completed the challenge", not a solver).

zu-cli already depends on zu-shadow lazily, so the dependency points one way
(cli -> shadow). The imports are inside the functions to keep that lazy.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4


def _semantic_target(tool: str, args: dict) -> Any:
    """A semantic ``{role, name, label}`` target for the rescued step — named by
    WHAT it acts on (the tool + a human label), never a selector/coordinate, so it
    is re-resolvable and feeds the §4 locator / §5 recognizer like any Shadow
    target."""
    from zu_shadow.capture import SemanticTarget

    label = tool.replace("_", " ")
    return SemanticTarget(role="button", name=f"human:{tool}", label=label)


def raw_inputs_for_rescue(run: Any, *, why: str | None) -> list[Any]:
    """The abstract input stream a rescue is recorded as — the same ``RawInput``
    shape a live human session produces, so the recorder folds it identically.

    The rescue is modelled as: navigate to where the run paused, then a single
    human action standing in for "the operator completed the gated/challenged step"
    — carrying the operator's redacted ``why`` as the demonstration's intent. For a
    captcha this is "a person solved the wall" (route, not defeat); for a human
    gate it is "a person approved the consequential step". No solver, no secret."""
    from zu_shadow.recorder import RawInput

    pending = getattr(run, "pending", {}) or {}
    tool = pending.get("tool") or "step"
    args = dict(pending.get("args") or {})
    url = str(args.get("url") or args.get("target") or "")
    target = _semantic_target(tool, args)
    items: list[Any] = []
    if url:
        items.append(RawInput(kind="navigate", url=url, intent=why))
    items.append(RawInput(kind="click", target=target, intent=why))
    return items


async def record_rescue(run: Any, *, why: str | None, by: str = "operator",
                        bus: Any = None) -> Any:
    """Fold a resolved rescue into a REDACTED Shadow ``RecordedSession`` — the input
    the synthesizer consumes. Builds its own in-memory bus by default so a
    demonstration never co-mingles with the rescued run's log. The "why" rides as
    the action intent and is redacted at capture, before append."""
    from zu_core.bus import EventBus
    from zu_shadow.recorder import Recorder

    bus = bus or EventBus()
    site = getattr(getattr(run, "spec", None), "target", None) or "rescue"
    recorder = Recorder(bus, site=str(site), trace_id=uuid4(), task_id=uuid4())
    stream = raw_inputs_for_rescue(run, why=why)
    outcome = f"human {by} resolved the escalation ({getattr(run, 'reason', 'human')})"
    return await recorder.record_stream(stream, outcome=outcome)


def demonstration_from_rescue(run: Any, *, why: str | None, by: str = "operator") -> dict:
    """A small, JSON-able record of the recorded demonstration for the
    /apprenticeship review feed — the redacted "why", the rescued step, and the
    explicit promotion posture (review-gated, NOT promoted). Synchronous + cheap so
    the resolve path can call it inline; the full Shadow recording is built on
    demand by ``record_rescue`` / ``synthesize_rescue``."""
    from zu_shadow.redaction import redact_text

    pending = getattr(run, "pending", {}) or {}
    return {
        "run_id": getattr(run, "run_id", None),
        "reason": getattr(run, "reason", "human"),
        "tool": pending.get("tool"),
        "why": redact_text(why) if why else None,  # redacted operator intent
        "by": by,
        "promoted": False,  # NEVER auto-promoted — review-gated downstream
        "status": "recorded-for-review",
    }


async def synthesize_rescue(run: Any, provider: Any, *, why: str | None,
                            instruction: str | None = None) -> Any:
    """Record the rescue and run the Shadow synthesizer over it to PROPOSE a
    rescue-derived agent. Returns the ``SynthesisResult`` — a PROPOSAL only; it is
    not agent behavior until ``verify_and_gate`` clears it. The operator's "why" is
    surfaced in ``intents_for_review``, never auto-applied."""
    from zu_shadow.synthesizer import Synthesizer

    session = await record_rescue(run, why=why)
    instr = instruction or f"Reproduce the human rescue of: {getattr(run, 'reason', 'an escalation')}"
    return await Synthesizer(provider).synthesize(session, instr), session


__all__ = [
    "demonstration_from_rescue",
    "raw_inputs_for_rescue",
    "record_rescue",
    "synthesize_rescue",
]
