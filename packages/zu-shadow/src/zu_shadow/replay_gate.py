"""The verification-replay PROMOTION GATE — a synthesized agent earns the right to run.

The rule is absolute: a generated agent does NOT run on real data until it
reproduces the recorded outcome. This gate is how that rule is enforced. It REUSES
the zu-cli offline machinery — :func:`zu_cli.offline.replay_offline` (Bundle /
FixtureSessionBackend) and the resilience gate behind :func:`zu_cli.build.build_offline`
— so the synthesized agent is exercised through exactly the same offline keystone
every other Zu agent is, at $0: no model, no network.

Promotion is BLOCKED unless the replayed run both SUCCEEDS and reproduces the
recorded outcome (the human's stated result, or the result value the recording
captured). A non-reproducing agent — one whose value diverges from the recording —
is held back with a clear reason; it never reaches real data. The captured "why"
resolutions are surfaced for REVIEW alongside the verdict; they are NEVER part of
the gate's auto-promotion decision.

The gate is parameterized by a ``runner`` (default :func:`replay_offline`) so the
offline tests can drive a scripted agent through it deterministically and prove both
the PASS (reproduces → promote) and the BLOCK (diverges → held) paths.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zu_core.contracts import Status

from .synthesizer import SynthesisResult

# The runner contract: (spec, cfg, bundle) -> (result, events). replay_offline fits.
Runner = Callable[[Any, Any, Any], Awaitable[tuple[Any, list]]]


@dataclass
class PromotionVerdict:
    """The gate's decision. ``promote`` is True ONLY when the replayed run succeeded
    AND reproduced the recorded outcome. ``intents_for_review`` carries the "why"
    resolutions for a human — never auto-applied."""

    promote: bool
    reproduced: bool
    status: str
    reason: str
    recorded_outcome: Any = None
    replayed_value: Any = None
    intents_for_review: list[tuple[int, str]] = field(default_factory=list)


def _recorded_outcome(session: Any) -> Any:
    """The outcome the recording asserts: the human's stated ``outcome`` if present,
    else the recorded ``session.end`` payload's outcome."""
    outcome = getattr(session, "outcome", None)
    if outcome is not None:
        return outcome
    from zu_core import events as ev

    for e in reversed(getattr(session, "events", [])):
        if getattr(e, "type", "") == ev.SHADOW_SESSION_END:
            return (getattr(e, "payload", {}) or {}).get("outcome")
    return None


def _reproduces(recorded: Any, value: Any) -> bool:
    """Did the replay reproduce the recorded outcome? When the recording stated no
    explicit outcome, a SUCCESS replay (checked by the caller) is itself the
    reproduced outcome — there is nothing more specific to match. When an outcome
    WAS stated, the replayed value must contain/equal it (string-contains is the
    generic, format-tolerant match — the recorded outcome is a human sentence)."""
    if recorded is None:
        return True
    rec = str(recorded).strip().lower()
    if not rec:
        return True
    return rec in str(value).strip().lower()


async def verify_and_gate(
    synthesis: SynthesisResult,
    session: Any,
    *,
    spec: Any,
    cfg: Any,
    bundle: Any,
    runner: Runner | None = None,
) -> PromotionVerdict:
    """Replay the synthesized agent against the recording's fixtures and gate
    promotion on the reproduced outcome.

    ``bundle`` is the offline fixtures projected from the recording (the same
    ``Bundle`` shape ``zu capture`` produces). The default ``runner`` is zu-cli's
    ``replay_offline`` — the exact offline keystone; tests inject a scripted runner.
    """
    if runner is None:
        from zu_cli.offline import replay_offline

        runner = replay_offline

    recorded = _recorded_outcome(session)
    try:
        result, _events = await runner(spec, cfg, bundle)
    except Exception as exc:  # noqa: BLE001 - a replay crash is a BLOCK, never a traceback
        return PromotionVerdict(
            promote=False, reproduced=False, status="error",
            reason=f"offline replay raised {type(exc).__name__}: {exc}",
            recorded_outcome=recorded,
            intents_for_review=list(synthesis.intents_for_review),
        )

    status = getattr(result, "status", None)
    value = getattr(result, "value", None)
    succeeded = status is Status.SUCCESS
    reproduced = succeeded and _reproduces(recorded, value)

    if not succeeded:
        reason = (f"offline replay did not succeed ({getattr(status, 'value', status)}: "
                  f"{getattr(result, 'reason', None)}) — held, not promoted")
    elif not reproduced:
        reason = (f"replay value {value!r} did not reproduce the recorded outcome "
                  f"{recorded!r} — held, not promoted")
    else:
        reason = "offline replay reproduced the recorded outcome — eligible for promotion"

    return PromotionVerdict(
        promote=reproduced,
        reproduced=reproduced,
        status=getattr(status, "value", str(status)),
        reason=reason,
        recorded_outcome=recorded,
        replayed_value=value,
        intents_for_review=list(synthesis.intents_for_review),
    )
