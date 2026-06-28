"""Bounded retry-on-stale — recover a detached element by IDENTITY (role+name+nth), in the
LIVE path, harness-owned and budget-bounded (navigation-reliability layer).

All $0 (no live browser, no network, no Docker): a fake session fails the first locate then
recovers after a re-capture, and a never-recovering fixture proves the retry is bounded and
escalates into the existing gated grounding/vision ladder rather than looping.
"""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.contracts import Budget, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import Severity
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.pointer import PointerControl
from zu_tools.rebind import stale_retries_max


class _RebindSession:
    """A fake session that fails ``recover_after`` locates (the element detached), answers
    op=axtree so the surface can be re-reduced, and records the op sequence."""

    def __init__(self, *, recover_after: int = 1, axtree_nodes: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self.locates = 0
        self.recover_after = recover_after
        self.axtree_nodes = axtree_nodes if axtree_nodes is not None else [
            {"role": {"value": "button"}, "name": {"value": "Menu"}, "ignored": False},
            {"role": {"value": "button"}, "name": {"value": "Buy"}, "ignored": False},
        ]

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        op = cmd["op"]
        if op == "axtree":
            return {"axtree": list(self.axtree_nodes), "title": "Shop", "url": ""}
        if op == "locate":
            self.locates += 1
            if self.locates <= self.recover_after:
                return {"error": "no such element"}
            return {"bounds": [400, 300, 80, 24], "cursor": [0.0, 0.0]}
        if op == "pointer":
            return {"dispatched": len(cmd["samples"]), "clicked": bool(cmd.get("click"))}
        return {}

    async def close(self) -> None:  # pragma: no cover - not used here
        pass


class _Ctx:
    def __init__(self, task_id: str, *, stale: int = 2) -> None:
        self.spec = type("S", (), {"task_id": task_id, "budget": Budget(stale_retries_max=stale)})()


class _CtxNoBudget:
    def __init__(self, task_id: str) -> None:
        self.spec = type("S", (), {"task_id": task_id})()


def _seed_handle_map(run_key: str, mapping: dict[str, dict]) -> None:
    """Stand in for action_surface(op=open): register a live entry + handle_map for the run."""
    from zu_tools import _session

    with _session._LOCK:
        _session._RUNS[run_key] = _session._RunEntry(handle=object())
    _session.put_handle_map(run_key, mapping)


def test_stale_retries_max_reads_from_ctx() -> None:
    class _C:
        spec = type("S", (), {"task_id": "t", "budget": Budget(stale_retries_max=5)})()

    assert stale_retries_max(_C()) == 5
    assert stale_retries_max(object()) == 0  # no budget -> disabled, never crashes


async def test_pointer_rebinds_stale_handle_and_succeeds() -> None:
    # The first locate misses (element detached); the harness re-captures, re-binds "Buy" by
    # identity to its FRESH handle (a2 — the surface renumbered), re-locates, and dispatches.
    sess = _RebindSession(recover_after=1)
    _seed_handle_map("run-rb", {"a1": {"role": "button", "name": "Buy"}})
    out = await PointerControl(session=sess, seed="s")(_Ctx("run-rb"), op="move_click", handle="a1")
    assert out["pointer"]["clicked"] is True
    assert out["pointer"]["handle"] == "a1"  # the model still only ever holds its opaque handle
    assert out["handle_rebound"] == [
        {"old_handle": "a1", "new_handle": "a2", "attempt": 1, "role": "button"}]
    # re-captured the surface (axtree) and re-located by identity, then dispatched
    assert [c["op"] for c in sess.sent] == ["locate", "axtree", "locate", "pointer"]


async def test_pointer_stale_retry_exhausts_then_escalates() -> None:
    # The element never re-locates: retries are bounded by stale_retries_max (2), then the tool
    # surfaces the stale handle AND flags the gated vision escalation — never an infinite loop.
    sess = _RebindSession(recover_after=99)
    _seed_handle_map("run-ex", {"a1": {"role": "button", "name": "Buy"}})
    out = await PointerControl(session=sess)(_Ctx("run-ex", stale=2), op="move_click", handle="a1")
    assert out["stale_handle"] == "a1"
    assert out["stale_exhausted"] is True
    assert len(out["handle_rebound"]) == 2
    # initial locate + two bounded (axtree, locate) attempts — and then it STOPS
    assert [c["op"] for c in sess.sent] == ["locate", "axtree", "locate", "axtree", "locate"]


async def test_pointer_stale_retry_gives_up_when_control_is_gone() -> None:
    # The control vanished from the surface entirely: re-bind gives up after one re-capture
    # (no matching identity) rather than burning the whole retry budget.
    sess = _RebindSession(recover_after=99, axtree_nodes=[])
    _seed_handle_map("run-gone", {"a1": {"role": "button", "name": "Buy"}})
    out = await PointerControl(session=sess)(_Ctx("run-gone", stale=3), op="move_click", handle="a1")
    assert out["stale_handle"] == "a1" and out["stale_exhausted"] is True
    assert len(out["handle_rebound"]) == 1 and out["handle_rebound"][0]["new_handle"] is None
    assert [c["op"] for c in sess.sent] == ["locate", "axtree"]


async def test_pointer_stale_retry_inert_without_budget() -> None:
    # Regression: with no stale-retry budget the pointer behaves exactly as before — an
    # immediate stale_handle escalation, no re-capture attempted.
    sess = _RebindSession(recover_after=99)
    _seed_handle_map("run-nob", {"a1": {"role": "button", "name": "Buy"}})
    out = await PointerControl(session=sess)(_CtxNoBudget("run-nob"), op="move_click", handle="a1")
    assert out["stale_handle"] == "a1"
    assert "handle_rebound" not in out and "stale_exhausted" not in out
    assert [c["op"] for c in sess.sent] == ["locate"]


def test_blind_detector_escalates_on_stale_exhaustion() -> None:
    # The exhausted-retry flag routes into the existing gated vision-escalation detector.
    from zu_checks.detectors.action_surface_blind import ActionSurfaceBlindDetector

    ctx = type("C", (), {"observation": {"stale_exhausted": True}})()
    verdict = ActionSurfaceBlindDetector().inspect(ctx)
    assert verdict is not None and verdict.detail is not None
    assert verdict.severity is Severity.ESCALATE and "re-bound" in verdict.detail


# --- the loop emission -------------------------------------------------------------------


class _RebindTool:
    """A tier-1 tool whose observation carries a ``handle_rebound`` list, to prove the loop turns
    it into data.handle.rebound events tool-agnostically."""

    name = "rebinder"
    tier = 1
    schema = {"name": "rebinder", "parameters": {"type": "object", "properties": {}}}
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx) -> dict:
        return {"pointer": {"handle": "a1", "clicked": True, "samples": 1, "duration_ms": 1.0,
                            "dest": {"x": 0, "y": 0}, "seed": "s"},
                "handle_rebound": [{"old_handle": "a1", "new_handle": "a2", "attempt": 1, "role": "button"}]}


async def test_loop_emits_handle_rebound_events() -> None:
    reg = Registry()
    reg.register("tools", "rebinder", _RebindTool())
    provider = ScriptedProvider.from_moves([{"tool": "rebinder", "args": {}}, {"text": "{}", "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status is Status.SUCCESS
    rebound = [e for e in await bus.query() if e.type == "data.handle.rebound"]
    assert len(rebound) == 1
    assert rebound[0].payload["old_handle"] == "a1" and rebound[0].payload["new_handle"] == "a2"
