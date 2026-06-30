"""#76 declarative action_policies + #74 declarative allowed_domains — both compile
to unbypassable pre-execution InvocationGates, proved offline ($0, ScriptedProvider,
no network/Docker). The event log is the oracle: GATE_DECIDED deny + the tool body
never running.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from zu_cli.config import ConfigError, RunConfig, build_registry
from zu_cli.policies import (
    AllowedDomainsGate,
    allowed_domains_invariant,
    compile_action_policies,
    compile_allowed_domains,
)
from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Event, Status, TaskSpec
from zu_core.invariants import predicate_holds
from zu_core.loop import run_task
from zu_core.ports import ToolCall
from zu_providers.scripted import ScriptedProvider

# --- a fake nav tool with an ``op`` discriminator + a write-shaped op ----------


class FakeBrowser:
    """Mirrors the real browser's shape (op enum, url arg, write_ops) but runs
    fully offline — records its calls so we can prove the body never executed."""

    name = "browser"
    tier = 1  # tier-1 so the scripted run can reach it with no escalation
    schema = {
        "name": "browser",
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["open", "act", "read", "close"]},
                "url": {"type": "string"},
            },
            "required": ["op"],
        },
    }
    prompt_fragment = "browser(op, url?)"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()
    write_ops = frozenset({"act"})

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, ctx, op, url=None, **kw) -> dict:
        self.calls.append((op, url))
        return {"text": f"did {op}", "url": url or "https://ok.example.com"}


def _reg(tool, *blocks):
    """A run registry with ``tool`` registered and the policy gates compiled from a
    RunConfig carrying the given blocks."""
    cfg_kwargs = {"provider": {"name": "scripted"}}
    for k, v in blocks:
        cfg_kwargs[k] = v
    cfg = RunConfig.model_validate(cfg_kwargs)
    from zu_core.registry import Registry

    reg = Registry()
    reg.register("tools", tool.name, tool)
    # reuse the real compiler path (validates + registers the gates/monitor)
    from zu_cli.config import _register_policy_gates

    _register_policy_gates(cfg, reg)
    return reg


# --- #76: action_policies ------------------------------------------------------


async def test_deny_rule_blocks_at_pre_exec_gate() -> None:
    tool = FakeBrowser()
    reg = _reg(tool, ("action_policies", [{"tool": "browser", "op": "act", "effect": "deny"}]))
    provider = ScriptedProvider.from_moves(
        [{"tool": "browser", "args": {"op": "act", "url": "https://x.example.com"}},
         {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)

    assert result.status == Status.SUCCESS
    assert tool.calls == []  # the gate blocked it before the body ran
    decided = [e for e in await bus.query() if e.type == ev.GATE_DECIDED]
    assert decided and decided[0].payload["decision"] == "deny"
    assert decided[0].payload["gate"] == "action_policies"
    assert any(e.type == ev.DEFENSE_BLOCKED for e in await bus.query())


async def test_allowed_op_proceeds() -> None:
    tool = FakeBrowser()
    reg = _reg(tool, ("action_policies", [{"tool": "browser", "op": "act", "effect": "deny"}]))
    provider = ScriptedProvider.from_moves(
        [{"tool": "browser", "args": {"op": "read"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS
    assert tool.calls == [("read", None)]  # a non-denied op runs


async def test_read_only_preset_blocks_act_allows_read() -> None:
    tool = FakeBrowser()
    reg = _reg(tool, ("action_policies", ["read-only"]))
    # act is write-shaped (in write_ops) -> denied; read proceeds
    provider = ScriptedProvider.from_moves(
        [{"tool": "browser", "args": {"op": "read"}},
         {"tool": "browser", "args": {"op": "act", "url": "https://x.example.com"}},
         {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert tool.calls == [("read", None)]  # read ran, act was gate-denied
    decided = [e for e in await bus.query() if e.type == ev.GATE_DECIDED]
    assert any(d.payload["decision"] == "deny" for d in decided)


async def test_first_match_wins_allow_overrides_later_deny() -> None:
    tool = FakeBrowser()
    reg = _reg(tool, ("action_policies", [
        {"tool": "browser", "op": "act", "effect": "allow"},
        {"tool": "browser", "effect": "deny"},
    ]))
    provider = ScriptedProvider.from_moves(
        [{"tool": "browser", "args": {"op": "act"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert tool.calls == [("act", None)]  # explicit allow short-circuited the deny


def test_unknown_tool_policy_raises_config_error_at_load() -> None:
    with pytest.raises(ConfigError, match="unknown tool"):
        compile_action_policies([{"tool": "nope", "effect": "deny"}], {"browser": FakeBrowser()})


def test_unknown_op_raises_config_error_at_load() -> None:
    with pytest.raises(ConfigError, match="unknown op"):
        compile_action_policies(
            [{"tool": "browser", "op": "frobnicate", "effect": "deny"}],
            {"browser": FakeBrowser()},
        )


def test_malformed_rule_and_bad_effect_raise() -> None:
    with pytest.raises(ConfigError):
        compile_action_policies([{"tool": "browser", "effect": "nope"}], {"browser": FakeBrowser()})
    with pytest.raises(ConfigError, match="unknown preset"):
        compile_action_policies(["allow-everything"], {"browser": FakeBrowser()})


# --- #74: allowed_domains ------------------------------------------------------


async def test_off_allowlist_navigation_denied_before_execution() -> None:
    tool = FakeBrowser()
    reg = _reg(tool, ("allowed_domains", ["*.good.example"]))
    provider = ScriptedProvider.from_moves(
        [{"tool": "browser", "args": {"op": "open", "url": "https://evil.test/x"}},
         {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS
    assert tool.calls == []  # off-allowlist host gate-denied before the body
    decided = [e for e in await bus.query() if e.type == ev.GATE_DECIDED]
    assert decided and decided[0].payload["decision"] == "deny"
    assert decided[0].payload["gate"] == "allowed_domains"


async def test_on_allowlist_navigation_proceeds() -> None:
    tool = FakeBrowser()
    reg = _reg(tool, ("allowed_domains", ["*.good.example"]))
    provider = ScriptedProvider.from_moves(
        [{"tool": "browser", "args": {"op": "open", "url": "https://api.good.example/v1"}},
         {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert tool.calls == [("open", "https://api.good.example/v1")]


def test_gate_and_invariant_derive_from_same_config() -> None:
    # #74 acceptance: the enforced allowlist and the post-hoc DOMAIN_ALLOWLIST
    # invariant come from the SAME pattern list and AGREE on the same hosts.
    patterns = compile_allowed_domains(["*.good.example", "api.partner.com"])
    assert patterns is not None
    gate = AllowedDomainsGate(patterns)
    inv = allowed_domains_invariant(patterns)
    # the invariant's allow-list IS the gate's pattern list
    assert inv.predicate.params["allow"] == patterns
    assert inv.predicate.params["wildcard"] is True

    def _fetched(host):
        return Event(trace_id=uuid4(), task_id=uuid4(), type="data.source.fetched",
                     source="t", payload={"url": f"https://{host}/p"})

    def _gate(host):
        return gate.check(ToolCall(name="browser", args={"op": "open", "url": f"https://{host}/p"}), None)

    # both agree: on-allowlist allowed (gate None, invariant holds), off-allowlist
    # denied (gate DENY, invariant fails) — from the SAME pattern list.
    assert _gate("a.good.example") is None
    assert predicate_holds(inv.predicate, [_fetched("a.good.example")]) is True
    assert _gate("evil.test") is not None
    assert predicate_holds(inv.predicate, [_fetched("evil.test")]) is False


def test_malformed_allowed_domains_raise_config_error() -> None:
    with pytest.raises(ConfigError):
        compile_allowed_domains(["https://example.com/path"])  # a URL, not a host
    with pytest.raises(ConfigError):
        compile_allowed_domains("notalist")  # not a list
    with pytest.raises(ConfigError):
        compile_allowed_domains([""])  # empty pattern


def test_build_registry_surfaces_policy_errors() -> None:
    # The validation must fire through the real assemble path (build_registry),
    # at load — not mid-run.
    cfg = RunConfig.model_validate({
        "provider": {"name": "scripted"},
        "plugins": {"tools": []},
        "action_policies": [{"tool": "http_fetch", "op": "act", "effect": "deny"}],
    })
    with pytest.raises(ConfigError):
        build_registry(cfg)
