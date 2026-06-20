"""embedded-widget — fires when the page's real content is inside a JS widget.

The complement to ``js-shell``. ``js-shell`` catches an *empty* SPA shell (a
``<div id="root">`` with no visible text). But a page can be full of human-visible
chrome — nav, footer, copy — while the data the task actually needs (appointment
slots, a price table, a seat map) is rendered by an **embedded third-party widget
or iframe** that loads via JavaScript. A tier-1 ``http_fetch`` sees the chrome and
the empty mount point, never the data, so it would loop forever or give up. This
detector is the deterministic signal to *offer* the browser (tier 2) in that case.

It is conservative about what counts as a content widget, to avoid escalating on
ubiquitous analytics/ad scripts:

* an ``<iframe>`` with an external ``http(s)`` ``src`` — an embedded application
  whose content is not in this DOM; or
* a **widget mount point** — an element whose *attributes* (id/class/data-*/domain)
  name a content widget (``widget``, ``embed``, ``scheduler``, or a known booking
  vendor) — together with an external ``<script>`` that fills it.

ESCALATE only *unlocks* the browser; the model renders only if it still lacks the
data, so being a touch generous here is cheap and fail-safe.
"""

from __future__ import annotations

import re

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _html_of

# Tokens that, when they appear in an element's ATTRIBUTES (not visible text),
# mark a JS content-widget mount. Generic structural words plus a few common
# booking/scheduling vendors — kept to attribute context so a nav link like
# href="/book-an-appointment" or body copy never trips it.
_WIDGET_TOKENS = (
    "widget", "embed", "scheduler", "data-widget",
    "vetstoria", "oabp", "calendly", "acuityscheduling", "simplybook", "petsapp",
)

# An <iframe ...> carrying an external http(s) src — an embedded app.
_IFRAME_SRC = re.compile(r"<iframe\b[^>]*\bsrc\s*=\s*[\"']https?://", re.IGNORECASE)
# Any element's attribute span, to scan for a widget token in attribute context.
_TAG_ATTRS = re.compile(r"<[a-zA-Z][a-zA-Z0-9]*\b([^>]*)>")
# An external <script src="http(s)://..."> — the loader that fills a mount point.
_EXTERNAL_SCRIPT = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']https?://", re.IGNORECASE)


def _has_widget_mount(html: str) -> bool:
    """True if some element's attributes name a content widget."""
    for m in _TAG_ATTRS.finditer(html):
        attrs = m.group(1).lower()
        if any(tok in attrs for tok in _WIDGET_TOKENS):
            return True
    return False


def _already_escalated(ctx: RunContext) -> bool:
    """True if the run has already escalated (or browser-rendered) this run.

    This detector is an escalation *trigger*: its job is to unlock the browser
    tier once. After that it must go quiet — every later widget page (another
    http_fetch, or the rendered DOM, which still carries the markers) would
    otherwise re-fire, and at the top tier a re-escalation is 'exhausted' and ENDS
    the run before the model can use the browser it just unlocked. So: fire once,
    then defer to the model working at the higher tier."""
    for ev in getattr(ctx, "events", []) or []:
        et = getattr(ev, "type", "")
        if et == "harness.task.escalated":
            return True
        if et == "data.source.fetched" and getattr(ev, "source", "") == "render_dom":
            return True
    return False


class EmbeddedWidgetDetector:
    name = "embedded-widget"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        if not html:
            return None
        if _already_escalated(ctx):
            return None  # already unlocked the browser; fire once, then stay quiet
        embedded_app = bool(_IFRAME_SRC.search(html))
        # A named mount point only counts when an external script is present to
        # fill it — a bare class="...widget..." on a static page isn't deferred.
        widget_loaded = _has_widget_mount(html) and bool(_EXTERNAL_SCRIPT.search(html))
        if embedded_app or widget_loaded:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="page defers content to an embedded widget/iframe; "
                       "escalate to a browser to render it",
            )
        return None
