"""js-shell — fires when a page is an empty JavaScript shell.

The canonical escalation trigger: tier-1 http_fetch returns HTML that is
essentially a <div id="root"></div> plus scripts, with no real text content.
That is the signal to give up on the cheap tier and climb to a browser.
Heuristics here are finalized against the graded fixture set in build step 5.
"""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _html_of

# Common SPA mount points / framework markers.
_SHELL_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", "__NEXT_DATA__")


class JsShellDetector:
    name = "js-shell"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        if not html:
            return None
        lowered = html.lower()
        looks_like_shell = any(m.lower() in lowered for m in _SHELL_MARKERS)
        script_heavy = lowered.count("<script") >= 1
        # crude visible-text proxy: strip tags is overkill here; use length of
        # text outside scripts as a later refinement (step 5).
        thin = len(html) < 4000
        if looks_like_shell and script_heavy and thin:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="page appears to be a JS shell; escalate to a browser",
            )
        return None
