"""bot-wall — fires on an anti-bot interstitial (Cloudflare, captcha, etc.)."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _contains_any, _html_of
from ._markers import CLOUDFLARE_FINGERPRINTS, STRONG_MARKERS, WEAK_MARKERS

# The wall marker sets are the ONE source of truth in ``._markers`` — a neutral
# module ``bot-wall`` and ``captcha`` both import, so neither detector depends on
# the other. See ``._markers`` for the strong/weak/fingerprint semantics.


class BotWallDetector:
    name = "bot-wall"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        strong = _contains_any(html, STRONG_MARKERS)
        weak = _contains_any(html, WEAK_MARKERS) and _contains_any(html, CLOUDFLARE_FINGERPRINTS)
        if strong or weak:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="anti-bot wall detected",
            )
        return None
