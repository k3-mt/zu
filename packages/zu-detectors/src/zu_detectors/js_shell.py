"""js-shell — fires when a page is an empty JavaScript shell.

The canonical escalation trigger: tier-1 http_fetch returns HTML that is
essentially a <div id="root"></div> plus scripts, with no real text content.
That is the signal to give up on the cheap tier and climb to a browser.

The test is structural, not size-based: a page is a shell when it has a known
SPA mount point *and* almost no human-visible text once scripts and styles are
removed. Measuring visible text (rather than raw HTML length) is what step 5
finalizes — a shell padded with a large inline bundle is still a shell, and a
small page that happens to be real content is not escalated.
"""

from __future__ import annotations

import re

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _contains_any, _html_of

# Common SPA mount points / framework markers.
_SHELL_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", "__NEXT_DATA__")

# Strip the elements whose contents are never visible text before measuring.
# ``\s*`` in the close tag tolerates ``</script >``; the second pattern handles
# an *unterminated* script/style — a browser treats everything after an unclosed
# <script> as script text, so the heuristic does too (consume to end of input).
# HTML comments are removed FIRST so a commented-out ``<!-- <script> -->`` (or
# any literal ``<script`` inside a comment) can't trip the greedy _UNCLOSED rule
# and erase the real article body after it — a deterministic false-positive the
# unbalanced-tag heuristic would otherwise produce.
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_NONVISIBLE = re.compile(r"<(script|style|template|noscript)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_UNCLOSED = re.compile(r"<(script|style|template|noscript)\b.*\Z", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# Below this many characters of visible text, a page with a mount point is
# treated as an unrendered shell. Tuned against the graded fixture set.
_MIN_VISIBLE_TEXT = 64


def _visible_text(html: str) -> str:
    """Human-visible text: drop script/style/template/noscript bodies, strip
    the remaining tags, and collapse whitespace."""
    without_code = _COMMENT.sub(" ", html)
    without_code = _NONVISIBLE.sub(" ", without_code)
    without_code = _UNCLOSED.sub(" ", without_code)
    text = _TAGS.sub(" ", without_code)
    return _WS.sub(" ", text).strip()


class JsShellDetector:
    name = "js-shell"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        if not html:
            return None
        lowered = html.lower()
        looks_like_shell = _contains_any(html, _SHELL_MARKERS)
        # The page defers its content to JS: a literal <script>, OR a module
        # graph pulled in via <link rel="modulepreload"> with no inline script
        # (a modern bundler shape the bare "<script" check would miss).
        script_heavy = "<script" in lowered or "modulepreload" in lowered
        thin = len(_visible_text(html)) < _MIN_VISIBLE_TEXT
        if looks_like_shell and script_heavy and thin:
            return Verdict(
                severity=Severity.ESCALATE,
                detector=self.name,
                detail="page appears to be a JS shell; escalate to a browser",
            )
        return None
