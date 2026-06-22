"""simulate — world-model foresight as a Tool (§9.2 path 1).

Proves the tool exposes the primitive without deciding: unconfigured it fails
loudly (never fabricates a rollout), and with a world model (sync or async) it
returns the prediction for the policy to reason over.
"""

from __future__ import annotations

from zu_tools.simulate import Simulate


async def test_unconfigured_fails_loudly() -> None:
    out = await Simulate()(None, state={"x": 1}, plan={"move": "right"})
    assert "no world model configured" in out["error"]


async def test_sync_simulator_returns_prediction() -> None:
    def world(state: dict, plan: dict) -> dict:
        return {"x": state["x"] + (1 if plan.get("move") == "right" else 0)}

    out = await Simulate(world)(None, state={"x": 1}, plan={"move": "right"})
    assert out["prediction"] == {"x": 2}


async def test_async_simulator_is_awaited() -> None:
    async def world(state: dict, plan: dict) -> dict:
        return {"reward": 0.9, "plan": plan}

    out = await Simulate(world)(None, state={}, plan={"approach": "target"})
    assert out["prediction"]["reward"] == 0.9
    assert out["prediction"]["plan"] == {"approach": "target"}


def test_simulate_declares_least_privilege() -> None:
    tool = Simulate()
    assert tool.capabilities == frozenset()
    assert tool.egress == frozenset()
    assert tool.tier == 1
