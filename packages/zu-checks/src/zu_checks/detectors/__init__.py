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

    Concatenates *every* present content key (html, text, content) rather than
    returning only the first, so a marker detector is never blind to a tool that
    splits content across keys — the same all-keys view the ``empty`` detector
    uses, so the detectors agree on what "the content" is."""
    obs = getattr(ctx, "observation", None)
    if isinstance(obs, dict):
        parts = [v for k in _CONTENT_KEYS if isinstance(v := obs.get(k), str) and v]
        if parts:
            return "\n".join(parts)
    return ""


def _contains_any(html: str, markers) -> bool:
    """True if any marker (case-insensitive) appears in ``html`` — the shared
    substring scan behind the marker-list detectors (bot-wall, js-shell)."""
    lowered = html.lower()
    return any(marker in lowered for marker in markers)
