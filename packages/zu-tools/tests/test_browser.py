"""browser — the persistent-session tool, against a fake session backend.

The tool never runs a browser itself; it leases a persistent session through the
backend and drives it with op=open/act/read/close, holding the session across
calls. A fake backend/session proves the tool's contract — open leases + sends,
act/read reuse the SAME session, close tears it down, and a new open replaces a
prior session — with no Docker and no browser.
"""

from __future__ import annotations

from zu_tools.browser import Browser


class _FakeSession:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self.reply: dict = {"status": 200, "url": "https://x/", "text": "step one"}

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        return {**self.reply, "_last_op": cmd["op"]}

    async def close(self) -> None:
        self.closed = True


class _FakeBackend:
    def __init__(self) -> None:
        self.sessions: list[_FakeSession] = []
        self.specs: list[dict] = []

    async def open_session(self, spec: dict) -> _FakeSession:
        self.specs.append(spec)
        s = _FakeSession()
        self.sessions.append(s)
        return s


async def test_open_then_act_read_reuse_one_session() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)

    out = await tool(None, op="open", url="http://spa.test/", capture_network=True)
    assert out["rendered"] and out["text"] == "step one"
    assert len(backend.sessions) == 1
    assert backend.sessions[0].sent[0] == {"op": "open", "url": "http://spa.test/", "capture_network": True}

    await tool(None, op="act", actions=[{"click": "text=Next"}])
    await tool(None, op="read")
    # same session reused — open/act/read all went to the one session
    assert len(backend.sessions) == 1
    assert [c["op"] for c in backend.sessions[0].sent] == ["open", "act", "read"]
    assert backend.sessions[0].sent[1]["actions"] == [{"click": "text=Next"}]


async def test_act_before_open_is_an_error() -> None:
    out = await Browser(backend=_FakeBackend(), allow_private=True)(None, op="act", actions=[])
    assert "no open session" in out["error"]


async def test_a_new_open_replaces_and_closes_the_prior_session() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    await tool(None, op="open", url="http://a.test/")
    first = backend.sessions[0]
    await tool(None, op="open", url="http://b.test/")
    assert first.closed and len(backend.sessions) == 2  # prior torn down, new opened


async def test_close_tears_down_the_session() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    await tool(None, op="open", url="http://a.test/")
    out = await tool(None, op="close")
    assert out == {"closed": True} and backend.sessions[0].closed
    # after close, act errors (no session)
    assert "no open session" in (await tool(None, op="act"))["error"]


async def test_aclose_closes_a_lingering_session() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    await tool(None, op="open", url="http://a.test/")
    await tool.aclose()
    assert backend.sessions[0].closed


async def test_open_requires_a_url() -> None:
    assert "requires a url" in (await Browser(backend=_FakeBackend())(None, op="open"))["error"]


def test_browser_is_tier_2_with_open_egress() -> None:
    b = Browser()
    assert b.tier == 2 and "*" in b.egress


async def test_unknown_op() -> None:
    out = await Browser(backend=_FakeBackend())(None, op="frobnicate")
    assert "unknown op" in out["error"]


async def test_session_error_passed_through() -> None:
    # A command error from the server (e.g. nothing open) surfaces, not crashes.
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    await tool(None, op="open", url="http://a.test/")
    backend.sessions[0].reply = {"error": "no open page"}
    out = await tool(None, op="read")
    assert out == {"error": "no open page"}
