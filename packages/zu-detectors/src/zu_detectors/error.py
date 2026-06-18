"""error — fires on an HTTP error status in the observation."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

# Client errors that a retry can never fix: the resource is permanently absent,
# forbidden, or the request itself is rejected. Everything else >= 400 (429 rate
# limit, 408 timeout, 5xx) is transient and worth a RETRY.
_TERMINAL_STATUSES = frozenset({400, 401, 403, 404, 405, 410, 451})


class ErrorDetector:
    name = "error"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        obs = getattr(ctx, "observation", None)
        status = obs.get("status") if isinstance(obs, dict) else None
        if isinstance(status, int) and status >= 400:
            sev = Severity.TERMINAL if status in _TERMINAL_STATUSES else Severity.RETRY
            return Verdict(severity=sev, detector=self.name, detail=f"http {status}")
        return None
