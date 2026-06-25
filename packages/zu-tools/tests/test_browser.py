"""browser — the persistent-session tool, against a fake session backend.

The tool never runs a browser itself; it leases a persistent session through the
backend and drives it with op=open/act/read/close, holding the session across
calls. A fake backend/session proves the tool's contract — open leases + sends,
act/read reuse the SAME session, close tears it down, and a new open replaces a
prior session — with no Docker and no browser.
"""

from __future__ import annotations

from zu_tools.browser import Browser


class _Ctx:
    """A run context carrying a task_id — the run key the shared session registry
    keys on. The browser tests drive the PRODUCTION path (a real run), so the session
    is shared/torn-down through the module registry, not a per-tool backend."""

    def __init__(self, task_id: str = "run-browser") -> None:
        self.spec = type("S", (), {"task_id": task_id})()


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
    ctx = _Ctx()

    out = await tool(ctx, op="open", url="http://spa.test/", capture_network=True)
    assert out["rendered"] and out["text"] == "step one"
    assert len(backend.sessions) == 1
    assert backend.sessions[0].sent[0] == {"op": "open", "url": "http://spa.test/", "capture_network": True}

    await tool(ctx, op="act", actions=[{"click": "text=Next"}])
    await tool(ctx, op="read")
    # same session reused — open/act/read all went to the one session
    assert len(backend.sessions) == 1
    assert [c["op"] for c in backend.sessions[0].sent] == ["open", "act", "read"]
    assert backend.sessions[0].sent[1]["actions"] == [{"click": "text=Next"}]


async def test_act_before_open_is_an_error() -> None:
    out = await Browser(backend=_FakeBackend(), allow_private=True)(_Ctx(), op="act", actions=[])
    assert "no open session" in out["error"]


async def test_a_reopen_reuses_the_one_shared_run_session() -> None:
    # In a run, the session is shared per run: a re-open does NOT lease a second
    # container — it reuses the one shared session (the container re-navigates). One
    # live session per run is the cross-tool sharing invariant.
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    ctx = _Ctx()
    await tool(ctx, op="open", url="http://a.test/")
    await tool(ctx, op="open", url="http://b.test/")
    assert len(backend.sessions) == 1                  # ONE shared session, not two
    assert [c["op"] for c in backend.sessions[0].sent] == ["open", "open"]


async def test_close_tears_down_the_shared_session() -> None:
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    ctx = _Ctx()
    await tool(ctx, op="open", url="http://a.test/")
    out = await tool(ctx, op="close")
    assert out == {"closed": True} and backend.sessions[0].closed
    # after close, act errors (the run's shared session is gone)
    assert "no open session" in (await tool(ctx, op="act"))["error"]


async def test_open_requires_a_url() -> None:
    assert "requires a url" in (await Browser(backend=_FakeBackend())(_Ctx(), op="open"))["error"]


def test_browser_is_tier_2_with_open_egress() -> None:
    b = Browser()
    assert b.tier == 2 and "*" in b.egress


async def test_unknown_op() -> None:
    out = await Browser(backend=_FakeBackend())(_Ctx(), op="frobnicate")
    assert "unknown op" in out["error"]


async def test_session_error_passed_through() -> None:
    # A command error from the server (e.g. nothing open) surfaces, not crashes.
    backend = _FakeBackend()
    tool = Browser(backend=backend, allow_private=True)
    ctx = _Ctx()
    await tool(ctx, op="open", url="http://a.test/")
    backend.sessions[0].reply = {"error": "no open page"}
    out = await tool(ctx, op="read")
    assert out == {"error": "no open page"}
