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

    def __init__(self, text: str = "", present=None, near_ok: bool = False) -> None:
        self._text = text
        self.present = present  # None => every selector present
        self.near_ok = near_ok
        self.calls: list[tuple] = []

    def _has(self, sel) -> bool:
        return self.present is None or sel in self.present

    def evaluate(self, _js, arg=None):
        # The proximity search (with an arg) returns near_ok; the cleanup call
        # (no arg) returns None — matches _click_near's two evaluate calls.
        return self.near_ok if arg is not None else None

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


class _FakeCDPSession:
    """A fake ``page.context.new_cdp_session`` result: serves a fixed AX tree and
    records the Accessibility domain calls. Proves the axtree op enables the domain
    and returns the raw CDP ``nodes`` list verbatim."""

    def __init__(self, ax_nodes) -> None:
        self._ax_nodes = ax_nodes
        self.sent: list[str] = []

    def send(self, method, params=None):
        self.sent.append(method)
        if method == "Accessibility.getFullAXTree":
            return {"nodes": self._ax_nodes}
        return {}


class _FakeContext:
    def __init__(self, page) -> None:
        self._page = page

    def new_cdp_session(self, _page):
        return _FakeCDPSession(self._page.ax_nodes)


class _FakeRoleLocator:
    """A fake ``page.get_by_role(...).first`` exposing ``bounding_box``."""

    def __init__(self, box) -> None:
        self._box = box

    @property
    def first(self):
        return self

    def bounding_box(self):
        return self._box


class _FakeMouse:
    """Records the dispatched move/down/up stream — the trusted-input contract the
    pointer op streams (geometry + button pairing)."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def move(self, x, y):
        self.events.append(("move", x, y))

    def down(self):
        self.events.append(("down",))

    def up(self):
        self.events.append(("up",))


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
        # New-op fakes: an AX tree to serve, a role→box map to locate against, a
        # mouse to record the dispatched stream, and a PNG to screenshot.
        self.ax_nodes: list = []
        self.role_box: dict | None = None
        self.mouse = _FakeMouse()
        self.png: bytes = b"\x89PNG-fake"
        self.viewport_size = {"width": 1280, "height": 720}
        self.context = _FakeContext(self)

    def title(self):
        return "Fake Title"

    def get_by_role(self, role, name=None):
        # A box keyed by role (and name when given), else None → "no element".
        box = None
        if isinstance(self.role_box, dict):
            box = self.role_box.get((role, name)) or self.role_box.get(role)
        return _FakeRoleLocator(box)

    def screenshot(self, full_page=False):
        self.calls.append(("screenshot", full_page))
        return self.png

    @property
    def frames(self):
        return self._frames if self._frames is not None else [self]

    def locator(self, sel):
        return _Loc(self, sel, 1 if (self.present is None or sel in self.present) else 0)

    def evaluate(self, _js, arg=None):
        return None   # the fake doesn't run JS (controls list is empty in tests)

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
    result = bs._run_actions(page, [{"click": "text=X"}, {"click": "text=Y"}])
    assert result is not None
    err, soft = result
    assert "text=X" in err and soft is True            # a missed click is a SOFT miss
    assert ("click", "text=Y") not in page.calls


def test_run_actions_classifies_soft_vs_fatal() -> None:
    page = _FakePage()
    # a malformed/unknown action is FATAL — the request itself is wrong
    err, soft = bs._run_actions(page, [{"frobnicate": "x"}])
    assert "unknown action" in err and soft is False
    err, soft = bs._run_actions(page, ["not a dict"])
    assert "bad action" in err and soft is False
    # an element-targeting action that misses is SOFT (no-op, not a broken page)
    page.fail_on = "text=Gone"
    _, soft = bs._run_actions(page, [{"click": "text=Gone"}])
    assert soft is True


def test_click_near_targets_the_frame_that_has_the_match() -> None:
    # "click 1 near 'Number of pets'" must resolve in the frame whose proximity
    # search succeeds, and click the marked element there — not the other frame.
    other = _FakeFrame(present=set(), near_ok=False)                       # search fails here
    widget = _FakeFrame(present={bs._NEAR_MARK}, near_ok=True)             # search succeeds here
    page = _FakePage(frames=[other, widget])
    bs._click_near(page, "1", "Number of pets", 5000)
    assert ("click", bs._NEAR_MARK) in widget.calls
    assert not any(k == "click" for k, *_ in other.calls)


def test_run_actions_near_disambiguates_a_click() -> None:
    widget = _FakeFrame(present={bs._NEAR_MARK}, near_ok=True)
    page = _FakePage(frames=[widget])
    assert bs._run_actions(page, [{"click": "1", "near": "Number of pets"}]) is None
    assert ("click", bs._NEAR_MARK) in widget.calls


def test_looks_like_css_distinguishes_selectors_from_labels() -> None:
    for css in ("#onetrust", ".item-link", "[aria-label='x']", "button:has-text('1')",
                "input[type=radio]", "div > a"):
        assert bs._looks_like_css(css), css
    for label in ("Next", "Tue Jun 23", "10:15", "I am a new client",
                  "Consultation (incl. sick & injured)"):
        assert not bs._looks_like_css(label), label


def test_robust_text_click_resolves_in_a_frame_and_clicks_marker() -> None:
    # The JS resolves the label to the real clickable and marks it; the helper then
    # clicks the marked element in the frame whose search succeeded.
    other = _FakeFrame(present=set(), near_ok=False)
    widget = _FakeFrame(present={bs._CLICK_MARK}, near_ok=True)
    page = _FakePage(frames=[other, widget])
    assert bs._robust_text_click(page, "Tue Jun 23", 5000) is True
    assert ("click", bs._CLICK_MARK) in widget.calls
    assert not any(k == "click" for k, *_ in other.calls)


def test_robust_text_click_returns_false_when_nothing_matches() -> None:
    page = _FakePage(frames=[_FakeFrame(near_ok=False)])
    assert bs._robust_text_click(page, "nope", 5000) is False


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


def test_observe_returns_focused_view_and_current_text() -> None:
    page = _FakePage(text="visible slots 10:15", url="https://x/book")
    obs, cur = bs.observe(page, [], include_html=True)
    assert obs["text"].startswith("visible slots 10:15") and obs["url"] == "https://x/book"
    assert cur == "visible slots 10:15" and obs["html"].startswith("<html>")
    # controls are folded into the grounded text (the fake returns none, so no menu)
    captured = [{"url": "https://api/slots", "status": 200, "body": '{"d":["2026-06-24"]}'}]
    obs2, _ = bs.observe(page, captured)
    # bodies fold into `content` (groundable); `network` is metadata only (no body)
    assert "2026-06-24" in obs2["content"]
    assert obs2["network"] == [{"url": "https://api/slots", "status": 200,
                                "content_type": "", "bytes": len('{"d":["2026-06-24"]}')}]
    assert "body" not in obs2["network"][0]


def test_observe_diff_returns_only_what_changed() -> None:
    # With a prev_text (an `act`), the model sees only the NEW lines — the change /
    # the challenge — not the whole page re-sent.
    page = _FakePage(text="Step 1\nName field\nStep 2\nThis is required")
    obs, _ = bs.observe(page, [], prev_text="Step 1\nName field")
    assert "Step 2" in obs["text"] and "This is required" in obs["text"]
    assert "Name field" not in obs["text"]                      # unchanged lines omitted


def test_observe_diff_reports_no_change() -> None:
    page = _FakePage(text="same\nstuff")
    obs, _ = bs.observe(page, [], prev_text="same\nstuff")
    assert "no visible change" in obs["text"]


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


# --- the tier-3/§4/§5 ops: axtree / locate / pointer / screenshot ------------


def test_axtree_op_enables_domain_and_returns_raw_cdp_nodes() -> None:
    # The axtree op enables the Accessibility domain and returns the raw CDP nodes
    # verbatim (the harness owns normalisation) plus the page title/url.
    nodes = [{"role": {"value": "button"}, "name": {"value": "Buy"}, "ignored": False}]
    page = _FakePage(url="https://shop.test/")
    page.ax_nodes = nodes
    p = _FakePlaywright(page)
    state: dict = {"browser": None, "page": page, "captured": []}
    out, done = bs.handle_command(state, {"op": "axtree"}, p)
    assert not done
    assert out["axtree"] == nodes
    assert out["url"] == "https://shop.test/" and out["title"] == "Fake Title"


def test_axtree_op_opens_a_page_when_none_held_and_url_given() -> None:
    nodes = [{"role": {"value": "link"}, "name": {"value": "Home"}}]
    page = _FakePage(url="https://x/")
    page.ax_nodes = nodes
    p = _FakePlaywright(page)
    state: dict = {"browser": None, "page": None, "captured": []}
    out, _ = bs.handle_command(state, {"op": "axtree", "url": "https://x/"}, p)
    assert out["axtree"] == nodes
    assert state["page"] is page                       # navigated + held
    assert ("goto", "https://x/", "load") in page.calls


def test_axtree_op_renavigates_a_held_page_to_a_different_url() -> None:
    # A run reuses ONE shared session (the registry keys on host, so a same-host new
    # PATH reuses the container). axtree(op) with a DIFFERENT url must re-navigate the
    # held page, not silently keep the stale one.
    nodes = [{"role": {"value": "button"}, "name": {"value": "Buy"}}]
    page = _FakePage(url="https://x/a")
    page.ax_nodes = nodes
    p = _FakePlaywright(page)
    state: dict = {"browser": _FakeBrowser(page), "page": page, "captured": ["old"]}
    out, _ = bs.handle_command(state, {"op": "axtree", "url": "https://x/b"}, p)
    assert out["axtree"] == nodes
    assert ("goto", "https://x/b", "load") in page.calls   # re-navigated to the new url
    assert state["captured"] == []                          # captured network cleared


def test_axtree_op_does_not_renavigate_when_url_matches_held_page() -> None:
    nodes = [{"role": {"value": "button"}, "name": {"value": "Buy"}}]
    page = _FakePage(url="https://x/a")
    page.ax_nodes = nodes
    p = _FakePlaywright(page)
    state: dict = {"browser": _FakeBrowser(page), "page": page, "captured": []}
    bs.handle_command(state, {"op": "axtree", "url": "https://x/a"}, p)
    assert not any(c[0] == "goto" for c in page.calls)      # same url -> no re-navigation


def test_axtree_op_errors_with_no_page_and_no_url() -> None:
    out, _ = bs.handle_command({"browser": None, "page": None, "captured": []},
                               {"op": "axtree"}, _FakePlaywright(_FakePage()))
    assert "no open page" in out["error"]


def test_locate_op_resolves_role_name_to_bounds_and_cursor() -> None:
    page = _FakePage()
    page.role_box = {("button", "Place order"): {"x": 412.0, "y": 308.5, "width": 96.0, "height": 40.0}}
    state: dict = {"browser": None, "page": page, "captured": [], "cursor": [10.0, 10.0]}
    out, _ = bs.handle_command(
        state, {"op": "locate", "handle": "a3", "locator": {"role": "button", "name": "Place order"}},
        _FakePlaywright(page))
    assert out["bounds"] == [412.0, 308.5, 96.0, 40.0]
    assert out["cursor"] == [10.0, 10.0]


def test_locate_op_requires_a_locator_not_just_a_handle() -> None:
    page = _FakePage()
    out, _ = bs.handle_command({"browser": None, "page": page, "captured": []},
                               {"op": "locate", "handle": "a3"}, _FakePlaywright(page))
    assert "locator required" in out["error"]


def test_locate_op_missing_element_is_an_error_not_a_crash() -> None:
    page = _FakePage()
    page.role_box = {}  # no match
    out, _ = bs.handle_command({"browser": None, "page": page, "captured": []},
                               {"op": "locate", "locator": {"role": "button", "name": "Nope"}},
                               _FakePlaywright(page))
    assert "no element" in out["error"]


def test_pointer_op_streams_moves_then_click_and_updates_cursor() -> None:
    page = _FakePage()
    state: dict = {"browser": None, "page": page, "captured": [], "cursor": [0.0, 0.0]}
    samples = [{"x": 11.2, "y": 12.0, "dt": 0.0}, {"x": 460.1, "y": 327.8, "dt": 0.0}]
    out, _ = bs.handle_command(state, {"op": "pointer", "samples": samples, "click": True},
                               _FakePlaywright(page))
    assert out["dispatched"] == 2 and out["clicked"] is True
    assert out["cursor"] == [460.1, 327.8]
    assert state["cursor"] == [460.1, 327.8]            # next locate sees it
    kinds = [e[0] for e in page.mouse.events]
    assert kinds == ["move", "move", "down", "up"]      # button pairing, trusted stream


def test_pointer_op_without_click_only_moves() -> None:
    page = _FakePage()
    out, _ = bs.handle_command({"browser": None, "page": page, "captured": []},
                               {"op": "pointer", "samples": [{"x": 5.0, "y": 5.0, "dt": 0.0}],
                                "click": False}, _FakePlaywright(page))
    assert out["clicked"] is False
    assert [e[0] for e in page.mouse.events] == ["move"]


def test_screenshot_op_returns_base64_png() -> None:
    import base64

    page = _FakePage(url="https://shot.test/")
    page.png = b"\x89PNG\x0d\x0a-bytes"
    out, _ = bs.handle_command({"browser": None, "page": page, "captured": []},
                               {"op": "screenshot"}, _FakePlaywright(page))
    assert base64.b64decode(out["screenshot_b64"]) == page.png
    assert out["mime"] == "image/png" and out["width"] == 1280 and out["url"] == "https://shot.test/"


def test_unknown_op_lists_the_new_ops() -> None:
    out, _ = bs.handle_command({}, {"op": "frob"}, _FakePlaywright(_FakePage()))
    assert "axtree" in out["error"] and "pointer" in out["error"]
