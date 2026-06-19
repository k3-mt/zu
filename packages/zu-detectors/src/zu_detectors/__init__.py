"""Zu built-in detectors.

A detector inspects an observation and may return a Verdict. Verdict
severities (WARN, RETRY, ESCALATE, TERMINAL) map onto the loop's control flow:
ESCALATE is the deterministic signal that climbs the tier ladder. Detectors
are where escalation is decided — never improvised by the model.
"""


# What counts as page content in an observation, in preference order. The loop
# stores a fetched/rendered page under one of these keys (mirrors zu_core.loop's
# own ``_CONTENT_KEYS``); a detector must consult all of them or it goes blind to
# a tool that returns ``{"text": ...}`` / ``{"content": ...}`` instead of html.
# One source of truth, reused by ``empty`` too.
_CONTENT_KEYS = ("html", "text", "content")


def _html_of(ctx) -> str:
    """Best-effort extraction of the page content from a RunContext observation.

    Reads the first present content key (html, then text, then content) so the
    html/text/content shapes all reach the markup-based detectors equally."""
    obs = getattr(ctx, "observation", None)
    if isinstance(obs, dict):
        for key in _CONTENT_KEYS:
            value = obs.get(key)
            if value:
                return value
    return ""


def _contains_any(html: str, markers) -> bool:
    """True if any marker (case-insensitive) appears in ``html`` — the shared
    substring scan behind the marker-list detectors (bot-wall, js-shell)."""
    lowered = html.lower()
    return any(marker in lowered for marker in markers)
