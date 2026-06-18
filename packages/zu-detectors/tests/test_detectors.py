"""Smoke tests for the built-in detectors and their discovery.

The escalation-ladder logic is finalized against the graded fixture set in
build step 5; these lock the basic verdicts and the entry-point contract now.
"""

from __future__ import annotations

from zu_core.ports import RunContext, Severity
from zu_core.registry import Registry
from zu_detectors.bot_wall import BotWallDetector
from zu_detectors.empty import EmptyDetector
from zu_detectors.error import ErrorDetector
from zu_detectors.js_shell import JsShellDetector


def _ctx(observation: dict) -> RunContext:
    return RunContext(spec=None, observation=observation)


def test_empty_fires_on_blank() -> None:
    v = EmptyDetector().inspect(_ctx({"html": "   "}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_empty_passes_on_content() -> None:
    assert EmptyDetector().inspect(_ctx({"html": "<p>hi</p>"})) is None


def test_error_terminal_on_404() -> None:
    v = ErrorDetector().inspect(_ctx({"status": 404, "html": ""}))
    assert v is not None and v.severity is Severity.TERMINAL


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


def test_detectors_discoverable() -> None:
    reg = Registry()
    reg.discover()
    for name in ("empty", "error", "js-shell", "bot-wall"):
        assert name in reg.names("detectors")
