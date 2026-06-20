"""Smoke tests for the built-in detectors and their discovery.

The escalation-ladder logic is finalized against the graded fixture set in
build step 5; these lock the basic verdicts and the entry-point contract now.
"""

from __future__ import annotations

from zu_checks.detectors.bot_wall import BotWallDetector
from zu_checks.detectors.empty import EmptyDetector
from zu_checks.detectors.error import ErrorDetector
from zu_checks.detectors.js_shell import JsShellDetector
from zu_core.ports import RunContext, Severity
from zu_core.registry import Registry


def _ctx(observation: dict) -> RunContext:
    return RunContext(spec=None, observation=observation)


def test_empty_fires_on_blank() -> None:
    v = EmptyDetector().inspect(_ctx({"html": "   "}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_empty_passes_on_content() -> None:
    assert EmptyDetector().inspect(_ctx({"html": "<p>hi</p>"})) is None


def test_empty_ignores_non_page_observations() -> None:
    # Regression: a successful html_parse result (no content key) must NOT be
    # read as an "empty page" and escalate — that misfired after real extraction.
    assert EmptyDetector().inspect(_ctx({"selector": "h1", "matches": ["X"], "count": 1})) is None
    assert EmptyDetector().inspect(_ctx({"error": "boom"})) is None
    assert EmptyDetector().inspect(_ctx({})) is None


def test_error_terminal_on_404() -> None:
    v = ErrorDetector().inspect(_ctx({"status": 404, "html": ""}))
    assert v is not None and v.severity is Severity.TERMINAL


def test_error_terminal_on_permanent_client_errors() -> None:
    # 410 Gone / 451 / 400 / 405 won't be fixed by a retry — they're terminal.
    for status in (400, 405, 410, 451):
        v = ErrorDetector().inspect(_ctx({"status": status, "html": ""}))
        assert v is not None and v.severity is Severity.TERMINAL, status


def test_error_retry_on_transient() -> None:
    # 429 (rate limit) and 5xx are transient — RETRY, not TERMINAL.
    for status in (429, 500, 503):
        v = ErrorDetector().inspect(_ctx({"status": status, "html": ""}))
        assert v is not None and v.severity is Severity.RETRY, status


def test_js_shell_fires_on_empty_spa() -> None:
    html = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    v = JsShellDetector().inspect(_ctx({"html": html}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_js_shell_passes_on_real_content() -> None:
    html = "<html><body>" + ("<p>real content here</p>" * 500) + "</body></html>"
    assert JsShellDetector().inspect(_ctx({"html": html})) is None


def test_js_shell_fires_despite_large_inline_script() -> None:
    # A shell padded with a big inline bundle is still a shell: the visible-text
    # test sees through the script, where a raw-length check would be fooled.
    bundle = "var x=1;" * 2000  # ~16 KB of code, zero visible text
    html = f'<html><body><div id="app"></div><script>{bundle}</script></body></html>'
    v = JsShellDetector().inspect(_ctx({"html": html}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_js_shell_fires_on_unterminated_script() -> None:
    # Malformed/streamed HTML: a <script> that is never closed. A browser treats
    # everything after it as script text, so the visible-text test must too —
    # the page is still a shell, not real content.
    html = '<html><body><div id="root"></div><script>var x=1;' + ("a();" * 2000)
    v = JsShellDetector().inspect(_ctx({"html": html}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_js_shell_passes_on_small_but_real_page() -> None:
    # A mount point with genuine prose is rendered content, not a shell.
    html = (
        '<html><body><div id="root">'
        "<h1>Acme Widget</h1><p>The finest widget, in stock and ready to ship today.</p>"
        "</div><script src=/app.js></script></body></html>"
    )
    assert JsShellDetector().inspect(_ctx({"html": html})) is None


def test_bot_wall_fires_on_captcha() -> None:
    v = BotWallDetector().inspect(_ctx({"html": "<h1>Just a moment...</h1> please verify you are human"}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_bot_wall_does_not_fire_on_innocent_phrase() -> None:
    # A real article that happens to contain a weak phrase must NOT escalate
    # without a corroborating Cloudflare fingerprint (regression: loose match).
    page = _ctx({"html": "<article><h1>Just a moment in history</h1>"
                          "<p>Attention required: read the safety notice first.</p></article>"})
    assert BotWallDetector().inspect(page) is None


def test_bot_wall_fires_on_weak_phrase_with_cloudflare_fingerprint() -> None:
    page = _ctx({"html": "<title>Just a moment...</title>"
                         "<div class='cf-browser-verification'></div><!-- cf-ray: abc -->"})
    v = BotWallDetector().inspect(page)
    assert v is not None and v.severity is Severity.ESCALATE


def test_detectors_discoverable() -> None:
    reg = Registry()
    reg.discover()
    for name in ("empty", "error", "js-shell", "bot-wall"):
        assert name in reg.names("detectors")
