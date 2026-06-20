"""Unit tests for the shared browser primitives + the persistent session handler.

No real Chromium: a fake page/browser/playwright drive the generic logic, proving
there is NO site-specific behavior — just apply-actions-by-selector, observe
rendered text, and hold state across open→act→read→close commands.

    pytest images/render-chromium/test_browser_session.py
"""

from __future__ import annotations

import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _browser_session as bs  # noqa: E402

# --- fakes -------------------------------------------------------------------


class _Loc:
    """A fake Playwright locator: count() for frame resolution; .first.click/etc
    route the action back to the owning frame/page (matches loc.first.click())."""

    def __init__(self, owner, sel: str, n: int) -> None:
        self._owner = owner
        self._sel = sel
        self._n = n

    def count(self) -> int:
        return self._n

    def nth(self, _i):
        return self

    def is_visible(self) -> bool:
        return self._n > 0   # the fake treats a present selector as visible

    @property
    def first(self):
        return self

    def click(self, timeout=None):
        self._owner._act("click", self._sel)

    def fill(self, value, timeout=None):
        self._owner._act("fill", self._sel, value)

    def select_option(self, value, timeout=None):
        self._owner._act("select", self._sel, value)


class _FakeFrame:
    """A frame that 'contains' a fixed set of selectors. Actions raise if the
    selector isn't present here — so _frame_for must route to the frame that has
    it (the iframe-piercing behavior)."""

    def __init__(self, text: str = "", present=None) -> None:
        self._text = text
        self.present = present  # None => every selector present
        self.calls: list[tuple] = []

    def _has(self, sel) -> bool:
        return self.present is None or sel in self.present

    def inner_text(self, _sel: str) -> str:
        return self._text

    def locator(self, sel):
        return _Loc(self, sel, 1 if self._has(sel) else 0)

    def _act(self, kind, sel, *extra):
        self.calls.append((kind, sel, *extra))
        if not self._has(sel):
            raise RuntimeError(f"no element {sel!r} in this frame")

    def wait_for_selector(self, sel, timeout=None):
        self._act("wait_for", sel)


class _FakePage:
    """A page that is its own single main frame by default; pass ``frames`` for a
    multi-frame (iframe) page. ``present`` limits which selectors the main frame
    has (None => all)."""

    def __init__(self, text: str = "hello", url: str = "https://x/",
                 present=None, frames=None) -> None:
        self.url = url
        self._text = text
        self.calls: list[tuple] = []
        self.handlers: dict[str, object] = {}
        self.fail_on: str | None = None
        self.present = present
        self._frames = frames

    @property
    def frames(self):
        return self._frames if self._frames is not None else [self]

    def locator(self, sel):
        return _Loc(self, sel, 1 if (self.present is None or sel in self.present) else 0)

    def inner_text(self, _sel: str) -> str:
        return self._text

    def content(self) -> str:
        return f"<html>{self._text}</html>"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.calls.append(("goto", url, wait_until))
        return type("R", (), {"status": 200})()

    def on(self, event, handler):
        self.handlers[event] = handler

    def _maybe_fail(self, sel):
        if self.fail_on is not None and sel == self.fail_on:
            raise RuntimeError("not found")

    def _act(self, kind, sel, *extra):
        self.calls.append((kind, sel, *extra))
        self._maybe_fail(sel)

    def wait_for_selector(self, sel, timeout=None):
        self._act("wait_for", sel)

    def wait_for_timeout(self, ms):
        self.calls.append(("wait_ms", ms))


class _FakeBrowser:
    def __init__(self, page: _FakePage) -> None:
        self._page = page
        self.closed = False

    def new_page(self, viewport=None):
        return self._page

    def close(self):
        self.closed = True


class _FakePlaywright:
    """Both the factory's context manager AND the entered object (has .chromium)."""

    def __init__(self, page: _FakePage) -> None:
        self._browser = _FakeBrowser(page)
        self.chromium = type("C", (), {"launch": lambda _self, args=None: self._browser})()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- generic primitives ------------------------------------------------------


def test_run_actions_fire_in_order_and_report_failure() -> None:
    page = _FakePage()
    assert bs._run_actions(page, [{"click": "text=A"}, {"wait_ms": 50}]) is None
    assert page.calls == [("click", "text=A"), ("wait_ms", 50)]
    page.fail_on = "text=X"
    err = bs._run_actions(page, [{"click": "text=X"}, {"click": "text=Y"}])
    assert err is not None and "text=X" in err
    assert ("click", "text=Y") not in page.calls


def test_run_actions_pierces_into_a_child_iframe() -> None:
    # The widget's button lives in a cross-origin iframe, not the top frame. The
    # action must be routed into the frame that actually has the selector.
    main = _FakeFrame(text="page chrome", present=set())           # no Next here
    child = _FakeFrame(text="Step 1 of 4 Next", present={"text=Next"})  # the widget iframe
    page = _FakePage(frames=[main, child])
    assert bs._run_actions(page, [{"click": "text=Next"}]) is None
    assert ("click", "text=Next") in child.calls                  # clicked INSIDE the iframe
    assert ("click", "text=Next") not in main.calls               # not the top frame


def test_dismiss_consent_clicks_a_known_accept_button() -> None:
    # A frame holding a known consent accept button gets it clicked; the selector
    # that worked is returned. Curated platform patterns, no site logic.
    banner = _FakeFrame(text="cookies", present={"#onetrust-accept-btn-handler"})
    page = _FakePage(frames=[banner])
    sel = bs.dismiss_consent(page, attempts=1)
    assert sel == "#onetrust-accept-btn-handler"
    assert ("click", "#onetrust-accept-btn-handler") in banner.calls


def test_dismiss_consent_noop_when_no_banner() -> None:
    page = _FakePage(present=set(), frames=[_FakeFrame(present=set())])
    assert bs.dismiss_consent(page, attempts=1) is None


def test_open_auto_dismisses_consent() -> None:
    # On open, a consent wall is cleared automatically and reported.
    page = _FakePage(present={"#onetrust-accept-btn-handler"})
    p = _FakePlaywright(page)
    out, _ = bs.handle_command({"browser": None, "page": None, "captured": []},
                               {"op": "open", "url": "https://x/"}, p)
    assert out.get("consent_dismissed") == "#onetrust-accept-btn-handler"


def test_run_actions_capped() -> None:
    page = _FakePage()
    bs._run_actions(page, [{"wait_ms": 1}] * (bs._MAX_ACTIONS + 5))
    assert len(page.calls) == bs._MAX_ACTIONS


def test_observe_returns_text_url_and_optional_html_network() -> None:
    page = _FakePage(text="visible slots 10:15", url="https://x/book")
    obs = bs.observe(page, [], include_html=True)
    assert obs["text"] == "visible slots 10:15" and obs["url"] == "https://x/book"
    assert obs["html"].startswith("<html>")
    captured = [{"url": "https://api/slots", "status": 200, "body": '{"d":["2026-06-24"]}'}]
    obs2 = bs.observe(page, captured)
    # bodies fold into `content` (groundable); `network` is metadata only (no body)
    assert "2026-06-24" in obs2["content"]
    assert obs2["network"] == [{"url": "https://api/slots", "status": 200,
                                "content_type": "", "bytes": len('{"d":["2026-06-24"]}')}]
    assert "body" not in obs2["network"][0]


# --- session handler: state persists across commands -------------------------


def test_session_open_act_read_close_holds_state() -> None:
    page = _FakePage(text="step one")
    p = _FakePlaywright(page)
    state: dict = {"browser": None, "page": None, "captured": []}

    out, done = bs.handle_command(state, {"op": "open", "url": "https://x/"}, p)
    assert not done and out["status"] == 200 and out["text"] == "step one"
    assert state["page"] is page and state["browser"] is p._browser  # held

    page._text = "step two"   # the live page advanced
    out, done = bs.handle_command(state, {"op": "act", "actions": [{"click": "text=Next"}]}, p)
    assert not done and out["text"] == "step two"
    assert ("click", "text=Next") in page.calls

    out, done = bs.handle_command(state, {"op": "read"}, p)
    assert not done and out["text"] == "step two"

    out, done = bs.handle_command(state, {"op": "close"}, p)
    assert done and out == {"closed": True} and p._browser.closed
    assert state["browser"] is None


def test_session_act_before_open_is_an_error() -> None:
    state: dict = {"browser": None, "page": None, "captured": []}
    out, done = bs.handle_command(state, {"op": "act", "actions": []}, _FakePlaywright(_FakePage()))
    assert not done and "no open page" in out["error"]


def test_session_unknown_op() -> None:
    out, _ = bs.handle_command({}, {"op": "frob"}, _FakePlaywright(_FakePage()))
    assert "unknown op" in out["error"]


def test_session_capture_network_accumulates_across_commands() -> None:
    page = _FakePage()
    p = _FakePlaywright(page)
    state: dict = {"browser": None, "page": None, "captured": []}
    bs.handle_command(state, {"op": "open", "url": "https://x/", "capture_network": True}, p)
    # simulate the page firing a captured response after an action
    handler = page.handlers["response"]
    handler(type("Resp", (), {
        "headers": {"content-type": "application/json"},
        "request": type("Rq", (), {"resource_type": "xhr"})(),
        "url": "https://api/slots", "status": 200,
        "text": lambda self=None: '{"available_dates":["2026-06-24"]}',
    })())
    out, _ = bs.handle_command(state, {"op": "read"}, p)
    assert "2026-06-24" in out["content"] and out["network"][0]["url"] == "https://api/slots"


# --- serve loop --------------------------------------------------------------


def test_serve_processes_commands_then_closes_on_eof() -> None:
    page = _FakePage(text="served")
    instream = io.StringIO(
        json.dumps({"op": "open", "url": "https://x/"}) + "\n"
        + json.dumps({"op": "read"}) + "\n"
    )  # no close -> EOF ends the loop
    out = io.StringIO()
    rc = bs.serve(instream, out, playwright_factory=lambda: _FakePlaywright(page), idle_timeout=None)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert rc == 0 and len(lines) == 2
    assert lines[0]["status"] == 200 and lines[1]["text"] == "served"


def test_serve_close_ends_and_tears_down() -> None:
    page = _FakePage()
    pw = _FakePlaywright(page)
    instream = io.StringIO(
        json.dumps({"op": "open", "url": "https://x/"}) + "\n"
        + json.dumps({"op": "close"}) + "\n"
        + json.dumps({"op": "read"}) + "\n"   # after close: never processed
    )
    out = io.StringIO()
    bs.serve(instream, out, playwright_factory=lambda: pw, idle_timeout=None)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[-1] == {"closed": True} and pw._browser.closed   # stopped at close


def test_serve_bad_json_is_reported_not_fatal() -> None:
    page = _FakePage()
    instream = io.StringIO("not json\n" + json.dumps({"op": "open", "url": "https://x/"}) + "\n")
    out = io.StringIO()
    bs.serve(instream, out, playwright_factory=lambda: _FakePlaywright(page), idle_timeout=None)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert "bad json" in lines[0]["error"] and lines[1]["status"] == 200  # recovered
