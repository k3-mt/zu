"""The sandbox egress allowlist must reflect per-instance egress (issue #52).

``_egress_allowlist`` derives the hosts the contained run's proxy permits from
the configured tools' declared egress. A tool may declare its egress only on the
INSTANCE (e.g. ``web_search`` sets ``self.egress`` in ``__init__`` from its
connector's host) — there is no class-level attribute. ``build_registry``
registers a bare ``plugins.tools`` entry AS THE CLASS, so reading the envelope
off the raw registry entry saw ``egress=[]`` and silently dropped the host from
the allowlist; under ``containment: required`` the default-DROP proxy then 403s
the tool.

The fix materializes each tool before reading ``declared_envelope`` — mirroring
the loop (``loop._materialize`` + the ``ENVELOPE_DECLARED`` event) — so the
allowlist matches the audit event and covers ANY tool with dynamic per-instance
egress. These run offline ($0, no network, ScriptedProvider).
"""

from __future__ import annotations

from zu_cli.config import PluginsConfig, ProviderConfig, RunConfig, build_registry
from zu_cli.main import _egress_allowlist
from zu_core.contracts import TaskSpec
from zu_core.loop import run_task
from zu_providers.scripted import ScriptedProvider

# A fake tool whose egress is chosen PER INSTANCE (a non-default host), declared
# only in __init__ — the general shape #52 is about, not just web_search. It is
# referenced by import-path below so build_registry registers it as the class.
_CUSTOM_HOST = "host.example.internal"


class _PerInstanceEgress:
    """An off-box tool with a per-instance egress host and NO class-level egress.

    ``hasattr(_PerInstanceEgress, "egress")`` is False, so reading the envelope
    off the class yields ``egress=[]`` — exactly the divergence #52 fixes."""

    name = "per_instance_egress"
    tier = 1
    schema = {"name": "per_instance_egress", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "per_instance_egress()"
    capabilities = frozenset({"net"})

    def __init__(self) -> None:
        # Egress set on the instance — mirrors web_search's connector host.
        self.egress = frozenset({_CUSTOM_HOST})

    async def __call__(self, ctx: object) -> dict:
        return {"ok": True}


def test_bare_web_search_contributes_its_per_instance_egress() -> None:
    # web_search listed BARE in plugins.tools (no tiers, no args) → registered as
    # the class. Its egress lives only on the instance, so the allowlist must
    # materialize it to see api.exa.ai (the default Exa connector's host).
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["web_search"]),
    )
    assert _egress_allowlist(cfg) == ["api.exa.ai"]


def test_per_instance_egress_tool_is_materialized_before_reading_egress() -> None:
    # The generic case: any tool whose egress is derived per instance. Referenced
    # by import-path so build_registry registers the class (the failing path).
    ref = f"{__name__}:_PerInstanceEgress"
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=[ref]),
    )
    assert _egress_allowlist(cfg) == [_CUSTOM_HOST]


async def test_allowlist_matches_the_loops_envelope_declared_event() -> None:
    # The consistency #52 requires: the proxy allowlist must equal the egress the
    # loop records in its ENVELOPE_DECLARED audit event for the SAME config. The
    # loop materializes the registry's tools before reading the envelope; now so
    # does _egress_allowlist, so the two views agree.
    from zu_core.bus import EventBus

    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["web_search"]),
    )
    allowlist = _egress_allowlist(cfg)

    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    await run_task(TaskSpec(query="q"), provider, build_registry(cfg), bus)

    declared = [e for e in await bus.query() if e.type == "harness.envelope.declared"]
    assert len(declared) == 1
    egress_from_event = sorted(
        {host for tool in declared[0].payload["tools"].values() for host in tool["egress"]}
    )
    assert allowlist == egress_from_event == ["api.exa.ai"]
