"""The demos behind `zu demo` — shipped in the package, ready straight away.

Covers both demo types: the escalation arc (web, the full three-pillar story)
and the minimal loop (no tools, runs on the bare base). Both run offline; the
real-model path is wired and fails cleanly without a key.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zu_cli import demo
from zu_cli.main import app
from zu_core.contracts import Status

runner = CliRunner()


@pytest.mark.asyncio
async def test_escalation_arc_runs_to_a_grounded_success():
    provider = demo.DEMOS["escalation"]["scripted"]()
    result, bus, backend = await demo.run_arc(provider, kind="escalation")

    assert result.status is Status.SUCCESS
    assert result.value == {"name": "Acme Widget", "price": "$9.00"}

    events = await bus.query()
    types = [e.type for e in events]
    assert "harness.detector.fired" in types
    escalated = [e for e in events if e.type == "harness.task.escalated"]
    assert len(escalated) == 1
    assert (escalated[0].payload["from_tier"], escalated[0].payload["to_tier"]) == (1, 2)
    assert backend.launched and backend.destroyed == 1
    assert types[-1] == "harness.task.completed"


@pytest.mark.asyncio
async def test_minimal_arc_runs_with_no_tools():
    provider = demo.DEMOS["minimal"]["scripted"]()
    result, bus, backend = await demo.run_arc(provider, kind="minimal")

    assert result.status is Status.SUCCESS
    assert result.value == {"capital": "Paris"}
    assert backend is None  # no browser/sandbox in the minimal demo
    types = [e.type for e in await bus.query()]
    assert "harness.tool.invoked" not in types  # genuinely tool-free
    assert types[-1] == "harness.task.completed"


def test_zu_demo_escalation_offline_exits_zero():
    result = runner.invoke(app, ["demo"])  # default type = escalation
    assert result.exit_code == 0, result.output
    assert "ESCALATE 1→2" in result.output
    assert "RESULT   : success" in result.output
    assert "Acme Widget" in result.output


def test_zu_demo_minimal_offline_exits_zero():
    result = runner.invoke(app, ["demo", "--type", "minimal"])
    assert result.exit_code == 0, result.output
    assert "type     : minimal" in result.output
    assert "RESULT   : success" in result.output
    assert "Paris" in result.output


def test_zu_demo_unknown_type_is_rejected():
    result = runner.invoke(app, ["demo", "--type", "nope"])
    assert result.exit_code == 2
    assert "unknown demo type" in result.output


def test_zu_demo_real_provider_fails_cleanly_without_a_key():
    result = runner.invoke(
        app,
        ["demo", "--type", "minimal", "--provider", "anthropic", "--model", "claude-x",
         "--api-key-env", "ZU_ABSENT_KEY"],
    )
    assert result.exit_code == 1
    assert "provider : anthropic" in result.output
    assert "run failed" in result.output


def test_example_wrapper_runs_as_a_subprocess():
    demo_path = Path(__file__).resolve().parents[3] / "examples" / "killer_demo.py"
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(demo_path)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "ESCALATE 1→2" in proc.stdout
    assert "RESULT   : success" in proc.stdout
