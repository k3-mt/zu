"""action-surface-blind — escalate to vision when the grounding surface fails the act.

The Action Surface (Engineering Design §11) is a fast, cheap default for the
common case; its competence boundary is the trigger for the next tier — pixels
and a vision model. When the accessibility tree is too thin to trust, the tool
sets ``surface_blind`` on its observation rather than silently returning an
incomplete surface. This detector turns that signal into the deterministic
ESCALATE that climbs the ladder to tier-4 vision (§11.4) — escalation decided by
a detector, never improvised by the model.

It also catches the OTHER way the grounding surface fails an act: a bounded
retry-on-stale that could not re-bind a detached element by identity within budget
(``stale_exhausted``, the navigation-reliability layer). Same remedy — pixels can
locate what the a11y/handle path can't — so it routes to the same gated vision
escalation rather than looping or handing the model a selector.
"""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict


class ActionSurfaceBlindDetector:
    name = "action-surface-blind"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        obs = getattr(ctx, "observation", None)
        if not isinstance(obs, dict):
            return None
        # A bounded retry-on-stale that exhausted its budget re-binding a detached element:
        # the handle path can no longer ground the act, so climb to vision (§11.4 ladder),
        # deterministically, rather than loop or rely on the model to recover.
        if obs.get("stale_exhausted") is True:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="target handle could not be re-bound within the stale-retry budget; "
                       "escalate to vision",
            )
        if obs.get("surface_blind") is not True:
            return None
        # The blind signal comes from either tier: the a11y Action Surface (climb to
        # the vision tier) or, at the last tier, the vision surface itself (no tier-5
        # — escalate to a human, §4.3/§4.4). Read whichever produced it and word the
        # operator-facing reason to match, so the message is never misleading.
        is_vision = "vision_surface" in obs
        surface = obs.get("vision_surface") if is_vision else obs.get("action_surface")
        reason = surface.get("blind_reason") if isinstance(surface, dict) else None
        fallback = (
            "vision surface blind at the last perception tier; escalate to a human"
            if is_vision
            else "action surface too thin to trust; escalate to vision"
        )
        return Verdict(
            severity=Severity.ESCALATE,
            detector=self.name,
            detail=reason or fallback,
        )
