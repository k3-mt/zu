"""Auto-settle — the harness-owned, budget-bounded wait for a surface to quiesce
before/after an act (navigation-reliability layer).

Three layers, all $0 (no live browser, no network, no Docker):

* the generic ``settle`` helper — polls a session quiescence probe, bounded by a poll count
  derived from the budget, and NEVER hangs (a never-quiescent page stops at the budget; an
  unsupported probe is a single no-op);
* the Browser tool integration — op=open settles after navigation, op=act settles pre + post,
  and the whole thing is INERT when the run carries no settle budget (pre-layer behaviour);
* the loop emission — a ``settle`` observation key becomes ``data.settle.waited`` events.
"""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.contracts import Budget, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.browser import Browser
from zu_tools.settle import settle, settle_budget_ms


class _Sess:
    """A minimal session that replays scripted quiescence responses in order (clamping to the
    last once exhausted) and records what was sent."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.i = 0
        self.sent: list[dict] = []

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        if not self.responses:
            return {}
        r = self.responses[min(self.i, len(self.responses) - 1)]
        self.i += 1
        return r


# --- the generic settle helper -----------------------------------------------------------


async def test_settle_disabled_returns_none() -> None:
    # budget 0 disables settling entirely — the pre-layer behaviour, no probe sent.
    sess = _Sess([{"quiescent": True}])
    assert await settle(sess, budget_ms=0, phase="pre") is None
    assert sess.sent == []


async def test_settle_resolves_when_quiescent() -> None:
    sess = _Sess([{"quiescent": False}, {"quiescent": False}, {"quiescent": True}])
    out = await settle(sess, budget_ms=30, phase="pre", poll_ms=10)
    assert out == {"phase": "pre", "ms_waited": 30, "polls": 3, "reason": "quiescent"}


async def test_settle_resolves_when_surface_stops_mutating() -> None:
    # want_stable: two equal fingerprints in a row = the SPA settled (even if never "quiescent").
    sess = _Sess([{"quiescent": False, "fingerprint": "x"}, {"quiescent": False, "fingerprint": "x"}])
    out = await settle(sess, budget_ms=30, phase="post", want_stable=True, poll_ms=10)
    assert out is not None and out["reason"] == "stable" and out["polls"] == 2


async def test_settle_never_quiesces_is_bounded() -> None:
    # A page whose fingerprint keeps changing and never goes quiescent must STOP at the budget
    # — the non-negotiable "no unbounded wait". 30ms / 10ms poll => at most 3 probes.
    sess = _Sess([
        {"quiescent": False, "fingerprint": "a"},
        {"quiescent": False, "fingerprint": "b"},
        {"quiescent": False, "fingerprint": "c"},
        {"quiescent": False, "fingerprint": "d"},
    ])
    out = await settle(sess, budget_ms=30, phase="post", want_stable=True, poll_ms=10)
    assert out is not None and out["reason"] == "budget_exhausted" and out["polls"] == 3


async def test_settle_unsupported_probe_is_a_transparent_no_op() -> None:
    # A server that doesn't implement the probe (no "quiescent" key) is a transparent no-op:
    # None (no settle record, no event, no perturbation), after a single probe.
    sess = _Sess([{"error": "unknown op: quiescence"}])
    out = await settle(sess, budget_ms=1000, phase="pre", poll_ms=10)
    assert out is None
    assert len(sess.sent) == 1  # did not keep probing an unsupported server


def test_settle_budget_ms_reads_from_ctx() -> None:
    class _C:
        spec = type("S", (), {"task_id": "t", "budget": Budget(settle_ms_max=777)})()

    assert settle_budget_ms(_C()) == 777
    assert settle_budget_ms(object()) == 0  # no spec/budget -> disabled, never crashes


# --- the Browser tool integration --------------------------------------------------------


class _QSession:
    """A fake browser session that answers op=quiescence from a script and every other op with a
    normal page reply; records the op sequence so settle's placement can be asserted."""

    def __init__(self, quiescence: list[dict]) -> None:
        self.sent: list[dict] = []
        self.q = list(quiescence)
        self.qi = 0
        self.closed = False

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        if cmd.get("op") == "quiescence":
            if not self.q:
                return {}
            r = self.q[min(self.qi, len(self.q) - 1)]
            self.qi += 1
            return r
        return {"status": 200, "url": "https://x/", "text": "step one", "_last_op": cmd.get("op")}

    async def close(self) -> None:
        self.closed = True


class _QBackend:
    def __init__(self, session: _QSession) -> None:
        self._session = session
        self.specs: list[dict] = []

    async def open_session(self, spec: dict) -> _QSession:
        self.specs.append(spec)
        return self._session


class _Ctx:
    def __init__(self, settle_ms: int = 100, task_id: str = "run-settle") -> None:
        self.spec = type("S", (), {"task_id": task_id, "budget": Budget(settle_ms_max=settle_ms)})()


class _CtxNoBudget:
    def __init__(self, task_id: str = "run-nob") -> None:
        self.spec = type("S", (), {"task_id": task_id})()


async def test_browser_open_settles_after_navigation() -> None:
    sess = _QSession([{"quiescent": True, "fingerprint": "f1"}])
    out = await Browser(backend=_QBackend(sess), allow_private=True)(_Ctx(), op="open", url="http://spa.test/")
    assert out["rendered"] and out["text"] == "step one"
    assert out["settle"] == [{"phase": "post", "ms_waited": 50, "polls": 1, "reason": "quiescent"}]
    assert [c["op"] for c in sess.sent] == ["open", "quiescence"]


async def test_browser_act_settles_pre_and_post() -> None:
    sess = _QSession([{"quiescent": True}, {"quiescent": True}, {"quiescent": True}])
    tool = Browser(backend=_QBackend(sess), allow_private=True)
    ctx = _Ctx()
    await tool(ctx, op="open", url="http://spa.test/")
    out = await tool(ctx, op="act", actions=[{"click": "text=Next"}])
    assert [s["phase"] for s in out["settle"]] == ["pre", "post"]
    # open(+post settle), then act's pre settle, the act, then act's post settle
    assert [c["op"] for c in sess.sent] == ["open", "quiescence", "quiescence", "act", "quiescence"]


async def test_browser_act_settle_is_bounded_when_never_quiescent() -> None:
    # 100ms budget / 50ms poll => at most 2 probes per settle; a never-settling page can't hang.
    sess = _QSession([{"quiescent": False, "fingerprint": "a"}, {"quiescent": False, "fingerprint": "b"}])
    out = await Browser(backend=_QBackend(sess), allow_private=True)(_Ctx(settle_ms=100), op="open", url="http://x/")
    assert out["settle"][0]["reason"] == "budget_exhausted" and out["settle"][0]["polls"] == 2


async def test_browser_settle_inert_without_budget() -> None:
    # Regression: a ctx with no settle budget behaves exactly as before — no probe, no settle key.
    sess = _QSession([{"quiescent": True}])
    out = await Browser(backend=_QBackend(sess), allow_private=True)(_CtxNoBudget(), op="open", url="http://x/")
    assert "settle" not in out
    assert [c["op"] for c in sess.sent] == ["open"]


# --- the loop emission -------------------------------------------------------------------


class _SettleTool:
    """A tier-1 tool whose observation carries a ``settle`` list, to prove the loop turns it
    into data.settle.waited events tool-agnostically (no browser/ladder needed)."""

    name = "settler"
    tier = 1
    schema = {"name": "settler", "parameters": {"type": "object", "properties": {}}}
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx) -> dict:
        return {"text": "ok", "settle": [
            {"phase": "pre", "ms_waited": 0, "polls": 1, "reason": "quiescent"},
            {"phase": "post", "ms_waited": 50, "polls": 1, "reason": "stable"}]}


async def test_loop_emits_settle_waited_events() -> None:
    reg = Registry()
    reg.register("tools", "settler", _SettleTool())
    provider = ScriptedProvider.from_moves([{"tool": "settler", "args": {}}, {"text": "{}", "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status is Status.SUCCESS
    settled = [e for e in await bus.query() if e.type == "data.settle.waited"]
    assert [e.payload["phase"] for e in settled] == ["pre", "post"]
    assert settled[1].payload["reason"] == "stable"
