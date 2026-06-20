"""error — fires on an HTTP error status in the observation."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict


class ErrorDetector:
    name = "error"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        # An HTTP error on a FETCHED page is RECOVERABLE, not fatal. A single bad
        # url (a 403 WAF wall, a 404, a 5xx) says nothing about whether the RUN can
        # succeed — an agent that searches and tries several candidates must be
        # free to fetch the next one. Ending the whole run on one bad fetch (the
        # old TERMINAL behaviour) broke exactly that. So this is RETRY: it is
        # recorded and fed back, the model sees the error and chooses another
        # action, and a run that genuinely cannot proceed still ends via the
        # step/token budget — not by assuming the first url was the only one.
        obs = getattr(ctx, "observation", None)
        status = obs.get("status") if isinstance(obs, dict) else None
        if isinstance(status, int) and status >= 400:
            return Verdict(severity=Severity.RETRY, detector=self.name, detail=f"http {status}")
        return None
