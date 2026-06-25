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


def test_error_on_http_status_is_recoverable_not_terminal() -> None:
    # An HTTP error on a fetched page is RETRY, never TERMINAL: a single bad url
    # (403 WAF wall, 404, 410, 5xx, 429) must not end a run that can try another
    # candidate. A truly stuck run ends via budget instead.
    for status in (400, 403, 404, 405, 410, 429, 451, 500, 503):
        v = ErrorDetector().inspect(_ctx({"status": status, "html": ""}))
        assert v is not None and v.severity is Severity.RETRY, status


def test_error_quiet_on_success() -> None:
    assert ErrorDetector().inspect(_ctx({"status": 200, "html": "<p>ok</p>"})) is None


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


# --- embedded-widget: content deferred to a JS widget/iframe -----------------

_VETSTORIA = (
    "<html><body><h1>Park Vets</h1><p>Lots of normal page chrome here, nav, "
    "footer, plenty of visible text so this is NOT an empty shell.</p>"
    "<div id='oabp-widget' domain='booking.vetstoria.com'></div>"
    "<script src='https://booking.vetstoria.com/js/oabp-widget.js'></script>"
    "</body></html>"
)


def test_embedded_widget_fires_on_a_js_booking_widget() -> None:
    from zu_checks.detectors.embedded_widget import EmbeddedWidgetDetector

    v = EmbeddedWidgetDetector().inspect(_ctx({"html": _VETSTORIA}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_embedded_widget_fires_on_an_external_iframe_app() -> None:
    from zu_checks.detectors.embedded_widget import EmbeddedWidgetDetector

    html = "<html><body><p>book below</p><iframe src='https://book.example/app'></iframe></body></html>"
    v = EmbeddedWidgetDetector().inspect(_ctx({"html": html}))
    assert v is not None and v.severity is Severity.ESCALATE


def test_embedded_widget_quiet_on_a_plain_content_page_with_analytics() -> None:
    from zu_checks.detectors.embedded_widget import EmbeddedWidgetDetector

    # A real content page that merely loads an external analytics script and links
    # to a booking page must NOT escalate — the data is in the HTML.
    html = (
        "<html><body><h1>Opening hours</h1><p>Mon-Fri 9-5. Call 020 555 1234.</p>"
        "<a href='/book-an-appointment'>Book an appointment</a>"
        "<script src='https://www.googletagmanager.com/gtag/js'></script></body></html>"
    )
    assert EmbeddedWidgetDetector().inspect(_ctx({"html": html})) is None


def test_embedded_widget_fires_once_then_stays_quiet() -> None:
    # It's an escalation trigger: it unlocks the browser once, then must go quiet —
    # a later widget page (or the rendered DOM) re-firing at the top tier would end
    # the run as 'escalation exhausted' before the model can use the browser.
    import types

    from zu_checks.detectors.embedded_widget import EmbeddedWidgetDetector

    det = EmbeddedWidgetDetector()
    assert det.inspect(RunContext(spec=None, observation={"html": _VETSTORIA}, events=[])) is not None
    for prior in (
        types.SimpleNamespace(type="harness.task.escalated", source=None, payload={}),
        types.SimpleNamespace(type="data.source.fetched", source="render_dom", payload={}),
    ):
        ctx = RunContext(spec=None, observation={"html": _VETSTORIA}, events=[prior])
        assert det.inspect(ctx) is None


# --- human-handoff detectors: route to a PERSON (kind="human") ---------------


def test_captcha_routes_to_a_human_not_a_tier_climb() -> None:
    # The captcha detector is bot-wall's kind="human" sibling: same deterministic
    # signal, but it ROUTES to a person (route, not defeat) rather than climbing a
    # tier. The handoff/loop reads ``kind == "human"`` to pause for an operator.
    from zu_checks.detectors.human_gate import CaptchaDetector

    v = CaptchaDetector().inspect(
        _ctx({"html": "<h1>Just a moment...</h1> please verify you are human"})
    )
    assert v is not None
    assert v.severity is Severity.ESCALATE
    assert v.kind == "human"  # routes to a human, NOT a plain tier climb


def test_captcha_quiet_on_an_innocent_page() -> None:
    from zu_checks.detectors.human_gate import CaptchaDetector

    assert CaptchaDetector().inspect(_ctx({"html": "<p>opening hours: 9-5</p>"})) is None


def test_human_gate_inert_until_armed() -> None:
    # The generic gate fires ONLY on an explicitly declared human-only step, so it
    # is a no-op until a tool/config arms it — never a surprise pause.
    from zu_checks.detectors.human_gate import HumanGateDetector

    det = HumanGateDetector()
    assert det.inspect(_ctx({"html": "a normal page"})) is None
    assert det.inspect(_ctx({"text": "confirm the wire"})) is None


def test_human_gate_fires_with_reason_when_armed() -> None:
    from zu_checks.detectors.human_gate import HumanGateDetector

    v = HumanGateDetector().inspect(
        _ctx({"human_gate": True, "human_gate_reason": "yes, send the wire"})
    )
    assert v is not None
    assert v.severity is Severity.ESCALATE and v.kind == "human"
    assert "yes, send the wire" in (v.detail or "")
    # the ``requires_human`` alias arms it too
    v2 = HumanGateDetector().inspect(_ctx({"requires_human": True}))
    assert v2 is not None and v2.kind == "human"


async def test_captcha_detector_pauses_the_run_for_a_human() -> None:
    # End-to-end: the captcha detector's kind="human" verdict routes through the
    # loop to ``_pause_for_human`` — the run SUSPENDS (Status.PAUSED) and the
    # approval record holds the literal invocation that hit the wall, so the
    # handoff API can present it and a resume re-runs that exact call.
    from zu_checks.detectors.human_gate import CaptchaDetector
    from zu_core.bus import EventBus
    from zu_core.contracts import Status, TaskSpec
    from zu_core.loop import run_task
    from zu_providers.scripted import ScriptedProvider

    class CaptchaPage:
        name = "open_page"
        tier = 1
        schema = {"name": "open_page",
                  "parameters": {"type": "object", "properties": {"url": {"type": "string"}}}}
        prompt_fragment = "open_page(url)"
        capabilities: frozenset[str] = frozenset()
        egress: frozenset[str] = frozenset()

        async def __call__(self, ctx, **kw):  # noqa: ANN001, ANN003
            return {"html": "<h1>Just a moment...</h1> please verify you are human"}

    reg = Registry()
    reg.register("tools", "open_page", CaptchaPage())
    reg.register("detectors", "captcha", CaptchaDetector())
    bus = EventBus()
    provider = ScriptedProvider.from_moves(
        [{"tool": "open_page", "args": {"url": "https://site/login"}}]
    )
    r = await run_task(TaskSpec(query="log in"), provider, reg, bus)
    assert r.status is Status.PAUSED  # routed to a human, not a tier climb
    events = await bus.query()
    req = [e for e in events if e.type == "harness.approval.requested"][-1]
    assert req.payload["tool"] == "open_page"
    assert req.payload["args"] == {"url": "https://site/login"}
    assert req.payload["reason"] == "captcha"


def test_detectors_discoverable() -> None:
    reg = Registry()
    reg.discover()
    for name in ("empty", "error", "js-shell", "embedded-widget", "bot-wall",
                 "captcha", "human-gate"):
        assert name in reg.names("detectors")
