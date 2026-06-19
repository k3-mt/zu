"""bot-wall — fires on an anti-bot interstitial (Cloudflare, captcha, etc.)."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _contains_any, _html_of

# Strong markers: specific enough to an anti-bot interstitial that their mere
# presence is the signal (no legitimate article body contains them).
_STRONG_MARKERS = (
    "captcha",
    "are you a robot",
    "verify you are human",
    "cf-browser-verification",
)

# Weak markers: real Cloudflare wall phrasing, but common-enough English that a
# substring match alone false-positives (an article titled "Just a Moment in
# History", a banner reading "Attention required"). They fire ONLY when a
# Cloudflare fingerprint is also present, so a normal page is never escalated.
_WEAK_MARKERS = (
    "attention required",
    "just a moment",
)
_CLOUDFLARE_FINGERPRINTS = (
    "cloudflare",
    "cf-ray",
    "cf-browser-verification",
    "__cf",
    "/cdn-cgi/",
)


class BotWallDetector:
    name = "bot-wall"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        strong = _contains_any(html, _STRONG_MARKERS)
        weak = _contains_any(html, _WEAK_MARKERS) and _contains_any(html, _CLOUDFLARE_FINGERPRINTS)
        if strong or weak:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="anti-bot wall detected",
            )
        return None
