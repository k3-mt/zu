"""The generalised Policy port (Engineering Design §9.2).

Proves the seam is wired like every other port — it carries an interface major,
it has a registry group and a decorator, and a class that is observation-in /
action-out satisfies the runtime-checkable Protocol — so a world-model or
embodied policy plugs in exactly where an LLM does.
"""

from __future__ import annotations

from zu_core import Action, Capabilities, Observation, Policy, ToolSpec
from zu_core.ports import INTERFACE_VERSION
from zu_core.registry import GROUPS, Registry


def test_policies_is_a_versioned_port_with_a_group() -> None:
    assert INTERFACE_VERSION["policies"] == 1
    assert GROUPS["policies"] == "zu.policies"


def test_toolspec_shape() -> None:
    ts = ToolSpec(name="http_fetch", description="fetch a url", json_schema={"name": "http_fetch"})
    assert ts.name == "http_fetch"
    assert ts.json_schema == {"name": "http_fetch"}


class _WorldModelPolicy:
    """A non-LLM policy: observation in, control action out — no model SDK."""

    capabilities = Capabilities(native_tools=False)

    @property
    def model(self) -> str | None:
        return "world-model-v0"

    async def act(self, observation: Observation, tools: list[ToolSpec]) -> Action:
        return Action.command(actuator="gait", vector=[0.5, 0.0])


def test_world_model_satisfies_the_policy_protocol() -> None:
    p = _WorldModelPolicy()
    assert isinstance(p, Policy)  # runtime_checkable structural check


async def test_world_model_returns_a_control_action() -> None:
    action = await _WorldModelPolicy().act(Observation.from_text("lidar: clear"), [])
    assert action.kind == "command"
    assert action.payload == {"actuator": "gait", "vector": [0.5, 0.0]}


def test_policy_registers_in_its_group() -> None:
    reg = Registry()
    reg.register("policies", "world-model", _WorldModelPolicy)
    assert reg.names("policies") == ["world-model"]
    assert reg.get("policies", "world-model") is _WorldModelPolicy
