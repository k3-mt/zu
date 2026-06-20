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


class _FakeFrame:
    def __init__(self, text: str) -> None:
        self._text = text

    def inner_text(self, _sel: str) -> str:
        return self._text


class _FakePage:
    def __init__(self, text: str = "hello", url: str = "https://x/") -> None:
        self.url = url
        self._text = text
        self.calls: list[tuple] = []
        self.handlers: dict[str, object] = {}
        self.fail_on: str | None = None

    # observe()
    @property
    def frames(self):
        return [_FakeFrame(self._text)]

    def content(self) -> str:
        return f"<html>{self._text}</html>"

    # navigation / events
    def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.calls.append(("goto", url, wait_until))
        return type("R", (), {"status": 200})()

    def on(self, event, handler):
        self.handlers[event] = handler

    # actions
    def _maybe_fail(self, sel):
        if self.fail_on is not None and sel == self.fail_on:
            raise RuntimeError("not found")

    def click(self, sel, timeout=None):
        self.calls.append(("click", sel))
        self._maybe_fail(sel)

    def fill(self, sel, value, timeout=None):
        self.calls.append(("fill", sel, value))
        self._maybe_fail(sel)

    def select_option(self, sel, value, timeout=None):
        self.calls.append(("select", sel, value))
        self._maybe_fail(sel)

    def wait_for_selector(self, sel, timeout=None):
        self.calls.append(("wait_for", sel))
        self._maybe_fail(sel)

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
    assert obs2["network"] == captured and "2026-06-24" in obs2["content"]


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
