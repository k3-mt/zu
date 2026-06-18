"""Zu built-in detectors.

A detector inspects an observation and may return a Verdict. Verdict
severities (WARN, RETRY, ESCALATE, TERMINAL) map onto the loop's control flow:
ESCALATE is the deterministic signal that climbs the tier ladder. Detectors
are where escalation is decided — never improvised by the model.
"""


def _html_of(ctx) -> str:
    """Best-effort extraction of the page HTML from a RunContext observation."""
    obs = getattr(ctx, "observation", None)
    if isinstance(obs, dict):
        return obs.get("html") or ""
    return ""
