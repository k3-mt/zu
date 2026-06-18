"""error — fires on an HTTP error status in the observation."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict


class ErrorDetector:
    name = "error"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        obs = getattr(ctx, "observation", None)
        status = obs.get("status") if isinstance(obs, dict) else None
        if isinstance(status, int) and status >= 400:
            sev = Severity.TERMINAL if status in (401, 403, 404) else Severity.RETRY
            return Verdict(severity=sev, detector=self.name, detail=f"http {status}")
        return None
