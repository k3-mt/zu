"""bot-wall — fires on an anti-bot interstitial (Cloudflare, captcha, etc.)."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _contains_any, _html_of

_WALL_MARKERS = (
    "captcha",
    "are you a robot",
    "verify you are human",
    "cf-browser-verification",
    "attention required",
    "just a moment",
)


class BotWallDetector:
    name = "bot-wall"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        if _contains_any(_html_of(ctx), _WALL_MARKERS):
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="anti-bot wall detected",
            )
        return None
