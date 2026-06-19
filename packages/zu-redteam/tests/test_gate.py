"""The end-to-end gate: PASS a safe plugin, FAIL an unsafe one. This is the
proof the gate is real — it both clears a good plugin and stops a bad one."""

from __future__ import annotations

from zu_redteam.fixtures import StaticFetch, LeakyFetch, SecretEchoTool
from zu_redteam.gate import run_gate


def _gate(report, name):
    return next(r for r in report.results if r.name == name)


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
    report = await run_gate(
        "echo", plugins=[("tools", "read_secret", SecretEchoTool("ZU-PLANTED-SECRET-9f3a7c21"))],
        run_unit=False,
    )
    assert not report.passed
    assert _gate(report, "adversarial").status == "FAIL"


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
