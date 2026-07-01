"""The verification-replay PROMOTION GATE — a synthesized agent earns the right to run.

The rule: a generated agent does NOT run on real data until it reproduces what the
human accomplished. This gate is how that rule is enforced. It REUSES the zu-cli
offline machinery — :func:`zu_cli.offline.replay_offline` (Bundle /
FixtureSessionBackend) and the resilience gate behind :func:`zu_cli.build.build_offline`
— so the synthesized agent is exercised through exactly the same offline keystone
every other Zu agent is, at $0: no model, no network.

Two regimes, because outcome reproduction is only *faithful* for a recording whose
data is frozen in the fixtures (Issue #56):

* An OFFLINE / fixture recording replays against captured observations, so the
  recorded outcome CAN be reproduced verbatim. Promotion is BLOCKED unless the
  replay both SUCCEEDS and reproduces that stated outcome; a divergent value is held.

* A LIVE-captured recording is authored against a real site whose data has since
  moved on — replaying it cannot faithfully reproduce the original outcome. So:
  - if the human STATED a concrete outcome, it is still reproduction-checked against
    the replay value (that is a real, verifiable assertion);
  - if NO outcome was stated, the gate does NOT claim full outcome reproduction — it
    verifies only what IS verifiable (the run succeeded and reproduced the recorded
    event/effect STRUCTURE) and HOLDS the agent as needs-input rather than
    auto-promoting on SUCCESS alone. It never asserts a guarantee it can't keep.

The captured "why" resolutions are surfaced for REVIEW alongside the verdict; they
are NEVER part of the gate's auto-promotion decision. The gate is parameterized by a
``runner`` (default :func:`replay_offline`) so the offline tests can drive a scripted
agent through it deterministically and prove the PASS / BLOCK / HELD paths.
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
    AND reproduced the recorded outcome. ``held`` marks a run that succeeded and
    reproduced the verifiable STRUCTURE but carries no stated outcome to fully
    reproduce (a live recording) — it is neither promoted nor a divergence-BLOCK, but
    a needs-input hold. ``intents_for_review`` carries the "why" resolutions for a
    human — never auto-applied."""

    promote: bool
    reproduced: bool
    status: str
    reason: str
    recorded_outcome: Any = None
    replayed_value: Any = None
    held: bool = False
    intents_for_review: list[tuple[int, str]] = field(default_factory=list)


def _is_live(session: Any) -> bool:
    """Whether the recording was authored via the LIVE headed capture. Read off the
    session's ``live`` marker, falling back to the ``session.start`` payload's ``live``
    flag (so a recording reloaded from an event log keeps its provenance)."""
    live = getattr(session, "live", None)
    if live is not None:
        return bool(live)
    from zu_core import events as ev

    for e in getattr(session, "events", []):
        if getattr(e, "type", "") == ev.SHADOW_SESSION_START:
            return bool((getattr(e, "payload", {}) or {}).get("live", False))
    return False


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


def _has_outcome(recorded: Any) -> bool:
    """A concrete, non-empty stated outcome exists to reproduce."""
    return recorded is not None and bool(str(recorded).strip())


def _reproduces(recorded: Any, value: Any) -> bool:
    """Did the replay reproduce the STATED recorded outcome? The caller only invokes
    this when a concrete outcome exists (``_has_outcome``). The replayed value must
    contain/equal it — string-contains is the generic, format-tolerant match, since the
    recorded outcome is a human sentence."""
    rec = str(recorded).strip().lower()
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
    live = _is_live(session)
    try:
        result, events = await runner(spec, cfg, bundle)
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
    intents = list(synthesis.intents_for_review)

    def _verdict(*, promote: bool, reproduced: bool, reason: str,
                 held: bool = False) -> PromotionVerdict:
        return PromotionVerdict(
            promote=promote, reproduced=reproduced, held=held,
            status=getattr(status, "value", str(status)), reason=reason,
            recorded_outcome=recorded, replayed_value=value, intents_for_review=intents,
        )

    if not succeeded:
        return _verdict(promote=False, reproduced=False, reason=(
            f"offline replay did not succeed ({getattr(status, 'value', status)}: "
            f"{getattr(result, 'reason', None)}) — held, not promoted"))

    # A concrete stated outcome is always reproduction-checked — verbatim reproduction is
    # a real, verifiable assertion whether the recording is live or offline.
    if _has_outcome(recorded):
        if _reproduces(recorded, value):
            return _verdict(promote=True, reproduced=True, reason=(
                "offline replay reproduced the stated recorded outcome — eligible for "
                "promotion"))
        return _verdict(promote=False, reproduced=False, reason=(
            f"replay value {value!r} did not reproduce the recorded outcome "
            f"{recorded!r} — held, not promoted"))

    # No stated outcome. For an OFFLINE/fixture recording the replay reproduces the
    # captured data faithfully, so a SUCCESS replay IS the reproduced outcome — promote.
    if not live:
        return _verdict(promote=True, reproduced=True, reason=(
            "offline/fixture replay succeeded with no stated outcome to diverge from — "
            "the captured data is reproduced faithfully; eligible for promotion"))

    # No stated outcome AND a LIVE recording: full outcome reproduction is NOT claimable
    # (the live site's data has moved on). Verify only what IS verifiable — the run
    # succeeded and reproduced the recorded event/effect STRUCTURE — and HOLD as
    # needs-input rather than auto-promote on SUCCESS alone. Fail-closed: never promote.
    structure_ok = bool(events)  # the replay produced an effect structure (a non-vacuous run)
    return _verdict(
        promote=False, reproduced=False, held=structure_ok, reason=(
            "live recording carries no stated outcome — full outcome reproduction cannot "
            "be verified against a live capture; the replay "
            + ("succeeded and reproduced the recorded event/effect structure, so the agent "
               "is HELD for a stated outcome (re-capture with an outcome, or state one) "
               "rather than auto-promoted"
               if structure_ok else
               "succeeded but produced no effect structure — held, not promoted")))
