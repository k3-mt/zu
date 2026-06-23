"""ZU-CORE-1 — the policy cannot acquire a capability not handed to it.

The model emits tool-call *signals*; the harness dispatches them against the
ACTIVE tool set for the current tier. A call to a tool that was never registered,
or one that exists at a higher tier the run has not climbed to, reaches nothing —
the model gets an error observation, never a handle to the tool. Capability
acquisition is exclusively the harness's job.
"""

from __future__ import annotations

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider


class TierTwoTool:
    """A privileged tool that only exists at tier 2. The side effect proves
    whether the model managed to reach it before the run climbed."""

    name = "privileged"
    tier = 2
    schema = {"name": "privileged", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "privileged()"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, ctx) -> dict:
        self.calls += 1
        return {"ok": True}


async def test_call_to_ungranted_tool_reaches_nothing() -> None:
    reg = Registry()  # no tools registered at all
    provider = ScriptedProvider.from_moves(
        [{"tool": "credential_broker", "args": {}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS
    returned = [e for e in await bus.query() if e.type == ev.TOOL_RETURNED]
    assert returned and returned[0].payload["observation"] == {"error": "unknown tool: credential_broker"}


async def test_call_to_not_yet_unlocked_tier_tool_does_not_execute() -> None:
    # The tier-2 tool is registered but the run starts at tier 1 and never climbs;
    # a call to it falls into the unknown-tool branch — the ladder is enforced on
    # dispatch, so the privileged tool never executes.
    tool = TierTwoTool()
    reg = Registry()
    reg.register("tools", "privileged", tool)
    provider = ScriptedProvider.from_moves(
        [{"tool": "privileged", "args": {}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    # max_tier=1 so there is no tier to climb to: the tool stays unreachable.
    result = await run_task(TaskSpec(query="q", max_tier=1), provider, reg, bus)
    assert result.status == Status.SUCCESS
    assert tool.calls == 0  # never acquired/executed
    returned = [e for e in await bus.query() if e.type == ev.TOOL_RETURNED]
    assert any("unknown tool" in str(e.payload["observation"]) for e in returned)
