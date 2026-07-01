"""Model-supplied browser actions are validated at the tool boundary (issue #65 F52).

``render_dom`` and ``browser`` used to forward the ``actions`` list into the
sandbox verbatim — any dict, any type, any stray key. These tests lock the new
boundary validator (:mod:`zu_tools.action_schema`) AND its wiring into both tools:
a well-formed action still flows through; a malformed one is REFUSED with a clear
error and never forwarded. They fail on the old code, where a bad action reached
the session/sandbox unchecked.
"""

from __future__ import annotations

import pytest

from zu_tools.action_schema import validate_action, validate_actions
from zu_tools.browser import Browser
from zu_tools.render import RenderDom

# --- the pure validator ---------------------------------------------------

@pytest.mark.parametrize("action", [
    {"click": "text=Next"},
    {"fill": "#email", "value": "a@b.com"},
    {"select": "#opt", "value": "Dog"},
    {"wait_for": "text=Choose a time"},
    {"click": "1", "near": "Number of pets"},
    {"wait_ms": 500},
    {"wait_ms": 0},
])
def test_valid_actions_pass(action: dict) -> None:
    assert validate_action(action) is None


@pytest.mark.parametrize("action,needle", [
    ("text=Next", "must be an object"),          # not a dict
    ({}, "empty action"),                         # no op
    ({"scroll": "#x"}, "unknown action"),        # op not in allowed set
    ({"click": 5}, "must be a string"),          # selector wrong type
    ({"click": "   "}, "must not be empty"),     # blank selector
    ({"click": "#a", "foo": "bar"}, "unexpected field"),   # stray field
    ({"click": "#a", "fill": "#b"}, "multiple ops"),       # two ops
    ({"fill": "#a", "value": 3}, "must be a string"),      # value wrong type
    ({"wait_ms": "500"}, "must be an integer"),  # wait_ms wrong type
    ({"wait_ms": True}, "must be an integer"),   # bool is not an int here
    ({"wait_ms": -1}, "non-negative"),           # negative wait
    ({"wait_ms": 5, "click": "#a"}, "must not carry other fields"),  # mixed
])
def test_invalid_actions_refused(action, needle: str) -> None:
    err = validate_action(action)
    assert err is not None and needle in err


def test_validate_actions_reports_first_offender_with_index() -> None:
    err = validate_actions([{"click": "#ok"}, {"scroll": "#bad"}])
    assert err is not None and "action[1]" in err
    assert validate_actions([{"click": "#ok"}]) is None
    assert validate_actions("notalist") is not None  # a non-list is refused


# --- wiring into the tools ------------------------------------------------

class _Ctx:
    def __init__(self, task_id: str = "run-f52") -> None:
        self.spec = type("S", (), {"task_id": task_id})()


class _FakeSession:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.reply = {"status": 200, "url": "https://x/", "text": "ok"}

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        return {**self.reply, "_last_op": cmd["op"]}

    async def close(self) -> None:
        pass


class _FakeBackend:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []

    async def open_session(self, spec: dict) -> _FakeSession:
        s = _FakeSession()
        self.sessions.append(s)
        return s


async def test_browser_refuses_invalid_action_before_forwarding() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    ctx = _Ctx()
    await tool(ctx, op="open", url="http://spa.test/")
    out = await tool(ctx, op="act", actions=[{"click": "#ok"}, {"scroll": "#bad"}])
    assert out.get("blocked") == "invalid_action" and "action[1]" in out["error"]
    # The malformed batch never reached the session — only the open was sent.
    assert [c["op"] for c in backend.sessions[0].sent] == ["open"]


async def test_browser_forwards_valid_action() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    ctx = _Ctx()
    await tool(ctx, op="open", url="http://spa.test/")
    out = await tool(ctx, op="act", actions=[{"fill": "#email", "value": "a@b.com"}])
    assert "blocked" not in out
    assert backend.sessions[0].sent[-1]["actions"] == [{"fill": "#email", "value": "a@b.com"}]


class _RenderBackend:
    """A sandbox backend whose exec/launch record whether they ran — so the test
    proves a refused action never reaches launch/exec."""

    def __init__(self) -> None:
        self.launched = False
        self.execed = False

    async def launch(self, spec: dict) -> object:
        self.launched = True
        return object()

    async def exec(self, sandbox: object, call: object) -> dict:
        self.execed = True
        return {"status": 200, "html": "<html></html>", "url": "http://x/"}

    async def destroy(self, sandbox: object) -> None:
        pass


async def test_render_refuses_invalid_action_before_launch() -> None:
    backend = _RenderBackend()
    tool = RenderDom(backend=backend, allow_private=True)
    out = await tool(_Ctx(), url="http://page.test/", actions=[{"scroll": "#bad"}])
    assert out.get("blocked") == "invalid_action"
    # Refused at the boundary — no sandbox was ever leased for this render.
    assert backend.launched is False and backend.execed is False
