"""human-gate detectors — route a step to a PERSON (Verdict.kind="human").

These are the ``kind="human"`` siblings of the plain tier-climb detectors. A
plain ESCALATE climbs the capability ladder (the model tries harder with a
stronger tool); a ``kind="human"`` ESCALATE instead PAUSES the run and hands the
exact invocation to a human (ZU-CD-1/2) — the loop routes it through
``_pause_for_human`` and the handoff API surfaces it to an operator.

Two flavours live here:

  * ``CaptchaDetector`` — fires on an anti-bot / captcha interstitial (it reuses
    the SAME deterministic signal as ``bot-wall``), but routes to a human instead
    of climbing a tier. The stance is ROUTE, NOT DEFEAT: a person completes the
    challenge on a system the operator is entitled to use; Zu ships no solver.
  * ``HumanGateDetector`` — a generic, config-armed gate for a declared
    human-only step (e.g. a final "yes, send the wire"). It fires only when the
    observation carries an explicit ``human_gate`` marker, so it is inert until a
    tool/config arms it — never a surprise pause.
"""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _contains_any, _html_of
from .bot_wall import (
    _CLOUDFLARE_FINGERPRINTS,
    _STRONG_MARKERS,
    _WEAK_MARKERS,
)


class CaptchaDetector:
    """Routes an anti-bot / captcha wall to a human (kind="human").

    Shares ``bot-wall``'s deterministic detection signal — the difference is the
    DESTINATION: ``bot-wall`` climbs the tier ladder (try a real browser), this
    one suspends the run and hands the challenge to a person. Register exactly one
    of the two for a given agent (this one when a human is on call for captchas;
    ``bot-wall`` when the policy is to escalate the tooling tier instead)."""

    name = "captcha"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        strong = _contains_any(html, _STRONG_MARKERS)
        weak = _contains_any(html, _WEAK_MARKERS) and _contains_any(html, _CLOUDFLARE_FINGERPRINTS)
        if strong or weak:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="captcha / anti-bot wall — routing to a human (route, not defeat)",
                kind="human",
            )
        return None


class HumanGateDetector:
    """A generic, declared human-only step (kind="human").

    Inert by default: it fires ONLY when the observation explicitly declares a
    human gate, so a tool/config arms it for a specific consequential step (a
    final wire send, an irreversible publish). The observation shape that arms it,
    in preference order:

      * ``obs["human_gate"]`` truthy — a tool flags this step needs a person; or
      * ``obs["requires_human"]`` truthy.

    The optional ``obs["human_gate_reason"]`` (a string) rides into the verdict's
    detail so the operator console can show WHY a person is needed. There is no
    site-specific hardcoding here — the *tool* declares the gate; the detector
    just routes it."""

    name = "human-gate"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        obs = getattr(ctx, "observation", None)
        if not isinstance(obs, dict):
            return None
        armed = bool(obs.get("human_gate")) or bool(obs.get("requires_human"))
        if not armed:
            return None
        reason = obs.get("human_gate_reason")
        detail = "declared human-only step — routing to a human"
        if isinstance(reason, str) and reason:
            detail = f"{detail}: {reason}"
        return Verdict(
            severity=Severity.ESCALATE,
            detector=self.name,
            detail=detail,
            kind="human",
        )
