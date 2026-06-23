"""ZU-NET-1 — pluggable egress enforcement, proved offline via swappability.

The enforcement *mechanism* is a port: a SandboxBackend-agnostic driver installs
a default-deny policy with DNS gating through whatever conformant mechanism is
configured, and tears it down. Swapping the mechanism needs no core change — the
test drives the same contract through two interchangeable implementations.
"""

from __future__ import annotations

from zu_backends.egress_enforce import (
    DockerInternalNetEnforcement,
    ScriptedEnforcement,
    docker_net_policy,
)
from zu_core.ports import EgressEnforcement


async def _enforce_a_run(mechanism: EgressEnforcement, allowlist: list[str]) -> object:
    # The SandboxBackend-agnostic contract: install a default-deny policy that
    # pins DNS, run under it, then revoke on teardown.
    handle = await mechanism.apply(
        {"allowlist": allowlist, "dns": "pin", "proxy": {"host": "proxy", "ip": "10.0.0.2", "port": 8080}}
    )
    await mechanism.revoke(handle)
    return handle


async def test_scripted_enforcement_satisfies_the_contract() -> None:
    mech = ScriptedEnforcement()
    assert isinstance(mech, EgressEnforcement)  # structural conformance
    await _enforce_a_run(mech, ["api.example.com"])
    assert mech.applied and mech.applied[0]["allowlist"] == ["api.example.com"]
    assert mech.applied[0]["dns"] == "pin"  # DNS is gated, not ambient
    assert mech.revoked  # torn down on teardown


async def test_mechanism_is_swappable_without_core_change() -> None:
    # The exact same driver runs against a different mechanism — the swap the
    # ZU-NET-1 conformance test calls for (nftables <-> docker-internal-net etc.).
    for mech in (ScriptedEnforcement(), DockerInternalNetEnforcement()):
        assert isinstance(mech, EgressEnforcement)
        handle = await _enforce_a_run(mech, ["broker.internal"])
        assert handle is not None


def test_docker_net_policy_pins_proxy_and_gates_dns() -> None:
    # ZU-NET-1 closure: the policy pins the proxy by IP (so no resolver is needed
    # to reach it) and points DNS at a non-resolving nameserver (so the embedded
    # resolver cannot be a covert egress channel).
    policy = docker_net_policy(
        {"proxy": {"host": "zu-proxy", "ip": "10.0.0.5", "port": 8080}, "dns": "pin"}
    )
    assert policy["extra_hosts"] == {"zu-proxy": "10.0.0.5"}
    assert policy["dns"] == ["127.0.0.1"]  # no working resolver -> no DNS exfil


def test_docker_net_policy_accepts_explicit_resolvers() -> None:
    policy = docker_net_policy({"proxy": {"host": "p", "ip": "10.0.0.5"}, "dns": ["10.0.0.53"]})
    assert policy["dns"] == ["10.0.0.53"]


async def test_docker_enforcement_apply_returns_policy() -> None:
    mech = DockerInternalNetEnforcement()
    handle = await mech.apply(
        {"allowlist": ["x"], "proxy": {"host": "p", "ip": "10.0.0.5", "port": 8080}, "dns": "pin"}
    )
    assert handle["policy"]["extra_hosts"] == {"p": "10.0.0.5"}
    assert handle["policy"]["dns"] == ["127.0.0.1"]
