"""The end-to-end gate: PASS a safe plugin, FAIL an unsafe one. This is the
proof the gate is real — it both clears a good plugin and stops a bad one."""

from __future__ import annotations

from zu_redteam.fixtures import LeakyFetch, SecretEchoTool, StaticFetch
from zu_redteam.gate import GateSecrets, run_gate


def _gate(report, name):
    return next(r for r in report.results if r.name == name)


# --- issue #7: per-run randomized canaries, deterministic under a seed ---------


def test_gate_secrets_are_fresh_per_run_and_high_entropy() -> None:
    # Two unseeded gate runs draw DIFFERENT canaries — nothing constant to hardcode.
    a, b = GateSecrets.fresh(), GateSecrets.fresh()
    assert a.planted_secret != b.planted_secret
    assert a.probe_host != b.probe_host
    assert a.pdf_fingerprint != b.pdf_fingerprint
    # The planted secret is long enough to enable the exfil observer's encoded-form
    # matching (its 12-char floor) — not a short marker.
    assert len(a.planted_secret) >= 12
    # No source-visible fixed constant is reused: the old hardcoded values are gone.
    assert a.planted_secret != "ZU-PLANTED-SECRET-9f3a7c21"
    assert a.probe_host != "example.com"


def test_gate_secrets_are_deterministic_under_a_fixed_seed() -> None:
    # The test hook: a fixed seed derives the SAME triple every time, so a test can
    # assert against a known secret while the real gate stays unpredictable.
    a = GateSecrets.for_seed("fixed")
    b = GateSecrets.for_seed("fixed")
    assert a.planted_secret == b.planted_secret
    assert a.probe_host == b.probe_host
    assert a.pdf_fingerprint == b.pdf_fingerprint
    # A different seed derives a different triple.
    assert GateSecrets.for_seed("other").planted_secret != a.planted_secret


async def test_gate_passes_a_safe_tool() -> None:
    report = await run_gate(
        "safe", plugins=[("tools", "good_fetch", StaticFetch(name="good_fetch"))], run_unit=False
    )
    assert report.passed, report.render()
    assert _gate(report, "contract").status == "PASS"
    assert _gate(report, "interop").status == "PASS"
    assert _gate(report, "adversarial").status == "PASS"
    # No Docker in CI → the container gate is honestly skipped, not failed.
    assert _gate(report, "container").status == "SKIP"


async def test_gate_fails_a_tool_that_underdeclares_egress() -> None:
    report = await run_gate(
        "leaky", plugins=[("tools", "leaky_fetch", LeakyFetch())], run_unit=False
    )
    assert not report.passed
    adv = _gate(report, "adversarial")
    assert adv.status == "FAIL" and "egress" in adv.detail


async def test_gate_fails_a_tool_that_leaks_the_secret() -> None:
    # The gate plants a fresh per-run canary (issue #7): a tool that leaks THAT
    # secret is caught. A fixed seed keeps the test deterministic; the echoing tool
    # is handed the same derived secret the gate plants, so the leak is genuine.
    from zu_redteam.gate import GateSecrets

    secrets = GateSecrets.for_seed("test-leak")
    report = await run_gate(
        "echo", plugins=[("tools", "read_secret", SecretEchoTool(secrets.planted_secret))],
        run_unit=False, secrets=secrets,
    )
    assert not report.passed
    assert _gate(report, "adversarial").status == "FAIL"


async def test_dormant_pivot_probe_catches_a_plugin_that_pivots_on_the_trigger() -> None:
    # The two-phase controlled handler (RED_TEAM.md §6.1): a plugin benign until it
    # sees the production trigger, then phones home to the off-allowlist host. The
    # probe must FIRE — a probe that never catches a pivot is theatre.
    from zu_redteam.fixtures import DormantPivotPlugin
    from zu_redteam.gate import _dormant_pivot_probe

    finding = await _dormant_pivot_probe([("tools", "notes", DormantPivotPlugin())])
    assert finding.outcome == "breached"
    assert any("egress" in b for b in finding.breaches)  # caught reaching the C2


async def test_dormant_pivot_probe_passes_a_benign_plugin() -> None:
    # A plugin that does not pivot — it never reaches the off-allowlist handler —
    # is contained.
    from zu_redteam.gate import _dormant_pivot_probe

    finding = await _dormant_pivot_probe([("tools", "inert_fetch", StaticFetch(name="inert_fetch"))])
    assert finding.outcome == "contained"


async def test_controlled_handler_flips_benign_then_trigger() -> None:
    # Phase 1 benign, phase 2 (armed) carries the trigger.
    from zu_core.ports import RunContext
    from zu_redteam.fixtures import ControlledHandler

    h = ControlledHandler(arm_after=1)
    ctx = RunContext(spec=None)
    first = await h(ctx, "http://handler.test.invalid/")
    assert ControlledHandler.TRIGGER not in first["html"]  # phase 1: benign
    second = await h(ctx, "http://handler.test.invalid/")
    assert ControlledHandler.TRIGGER in second["html"]      # phase 2: trigger


def test_benign_args_fills_all_required_params() -> None:
    # A tool with non-url required params is filled in-type (previously only `url`
    # was inferred, so such a tool was probed with {} — an arg error proving nothing).
    from zu_redteam.gate import _benign_args

    class Search:
        schema = {"name": "search", "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"},
                           "site": {"type": "string"}},
            "required": ["query", "limit"]}}

    args, opaque = _benign_args(Search())
    assert args["query"] == "probe" and args["limit"] == 1
    assert "site" in args  # declared optional still filled
    assert opaque == []     # every required param had a known type


def test_benign_args_infers_url_shape_and_flags_opaque_required() -> None:
    from zu_redteam.gate import _benign_args

    class Weird:
        schema = {"name": "weird", "parameters": {
            "type": "object",
            "properties": {"endpoint": {"type": "string"}, "blob": {}},
            "required": ["endpoint", "blob"]}}

    args, opaque = _benign_args(Weird())
    assert args["endpoint"] == "http://probe.example/"  # url-shaped name -> benign URL
    assert opaque == ["blob"]  # untyped required param surfaced, not silently passed


async def test_gate_probes_a_tool_with_non_url_required_args() -> None:
    # End to end: a clean tool whose required param is NOT a url is genuinely
    # exercised by the directed probe and PASSes.
    class SearchTool:
        name = "search"
        tier = 1
        schema = {"name": "search", "description": "Search.",
                  "parameters": {"type": "object",
                                 "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}}
        prompt_fragment = "search(query)"
        capabilities: frozenset = frozenset()
        egress: frozenset = frozenset()

        async def __call__(self, ctx, query):
            return {"text": f"results for {query}"}

    report = await run_gate("search", plugins=[("tools", "search", SearchTool())], run_unit=False)
    assert report.passed, report.render()
    assert _gate(report, "adversarial").status == "PASS"
    assert any(f.id == "probe:search" for f in report.findings)  # the tool was probed


async def test_contract_gate_flags_a_tool_missing_its_envelope() -> None:
    class NoEnvelopeTool:
        name = "bad"
        tier = 1
        schema = {"name": "bad", "parameters": {"type": "object", "properties": {}}}
        prompt_fragment = "bad()"
        # deliberately omits capabilities/egress
        async def __call__(self, ctx):  # pragma: no cover - never invoked
            return {}

    report = await run_gate("bad", plugins=[("tools", "bad", NoEnvelopeTool())], run_unit=False)
    contract = _gate(report, "contract")
    assert contract.status == "FAIL" and "capabilities" in contract.detail


async def test_container_gate_skips_without_docker_and_probe_spec_is_valid(monkeypatch) -> None:
    # The container form is opt-in: without the flag (or Docker) it SKIPs, never
    # FAILs a plugin. And the benign probe spec the live path would run is itself
    # well-formed and clean when run offline — so the live path runs real work.
    from zu_redteam.gate import GateSecrets, _container_gate, _container_probe_spec
    from zu_redteam.runner import run_spec
    from zu_redteam.verdict import ObservedRun, default_observers

    secrets = GateSecrets.for_seed("test-container")
    monkeypatch.delenv("ZU_REDTEAM_CONTAINER", raising=False)
    result = await _container_gate(secrets)
    assert result.status == "SKIP"

    events = await run_spec(_container_probe_spec(secrets))
    run = ObservedRun.from_events(events, None, planted_secret=secrets.planted_secret)
    assert all(o.inspect(run) is None for o in default_observers())
