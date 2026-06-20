"""`zu demo` — proves runnability against a real model (key required by default).

Tests use `--offline` (the scripted, fixtured self-test) since CI has no key,
and assert the real path requires a model and is wired to a real adapter.
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


# --- the arc, offline (wiring self-test for each type) -----------------------


async def test_web_arc_offline_fetches_and_validates():
    provider = demo.DEMOS["web"]["scripted"]()
    result, bus, backend = await demo.run_arc(provider, kind="web", offline=True)
    assert result.status is Status.SUCCESS
    assert result.value == {"title": "Example Domain"}
    assert backend is None  # tier 1: no browser
    types = [e.type for e in await bus.query()]
    assert "harness.tool.invoked" in types  # a real tool ran (http_fetch)


async def test_minimal_arc_offline_no_tools():
    provider = demo.DEMOS["minimal"]["scripted"]()
    result, bus, _ = await demo.run_arc(provider, kind="minimal", offline=True)
    assert result.status is Status.SUCCESS
    assert result.value == {"capital": "Paris"}
    assert "harness.tool.invoked" not in [e.type for e in await bus.query()]


async def test_escalation_arc_offline_climbs_to_tier2():
    provider = demo.DEMOS["escalation"]["scripted"]()
    result, bus, backend = await demo.run_arc(provider, kind="escalation", offline=True)
    assert result.status is Status.SUCCESS
    escalated = [e for e in await bus.query() if e.type == "harness.task.escalated"]
    assert len(escalated) == 1
    assert (escalated[0].payload["from_tier"], escalated[0].payload["to_tier"]) == (1, 2)
    assert backend.launched and backend.destroyed == 1


async def test_real_escalation_is_not_available_without_docker_image():
    # The real tier-2 path is honest about the missing browser image.
    with pytest.raises(RuntimeError, match="needs Docker"):
        await demo.run_arc(demo.DEMOS["escalation"]["scripted"](), kind="escalation", offline=False)


# --- the CLI -----------------------------------------------------------------


def test_zu_demo_requires_a_model_by_default():
    result = runner.invoke(app, ["demo"])  # no --model, no --offline
    assert result.exit_code == 2
    assert "runs against a real model" in result.output


def test_zu_demo_offline_web_default_exits_zero():
    result = runner.invoke(app, ["demo", "--offline"])
    assert result.exit_code == 0, result.output
    assert "type     : web" in result.output
    assert "offline self-test" in result.output
    assert "Example Domain" in result.output


def test_zu_demo_offline_minimal_exits_zero():
    result = runner.invoke(app, ["demo", "--offline", "--type", "minimal"])
    assert result.exit_code == 0, result.output
    assert "Paris" in result.output


def test_zu_demo_offline_escalation_shows_climb():
    result = runner.invoke(app, ["demo", "--offline", "--type", "escalation"])
    assert result.exit_code == 0, result.output
    assert "ESCALATE 1→2" in result.output


def test_zu_demo_unknown_type_is_rejected():
    result = runner.invoke(app, ["demo", "--offline", "--type", "nope"])
    assert result.exit_code == 2
    assert "unknown demo type" in result.output


def test_zu_demo_real_provider_fails_cleanly_without_a_key():
    result = runner.invoke(
        app,
        ["demo", "--type", "minimal", "--model", "claude-x",
         "--provider", "anthropic", "--api-key-env", "ZU_ABSENT_KEY"],
    )
    assert result.exit_code == 1
    assert "provider : anthropic" in result.output
    assert "run failed" in result.output


def test_zu_demo_real_escalation_reports_missing_docker_path():
    result = runner.invoke(
        app,
        ["demo", "--type", "escalation", "--model", "claude-x",
         "--provider", "anthropic", "--api-key", "sk-fake"],
    )
    assert result.exit_code == 1
    assert "needs Docker" in result.output


def test_example_wrapper_offline_subprocess():
    demo_path = Path(__file__).resolve().parents[3] / "examples" / "killer_demo.py"
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(demo_path), "--offline"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "Example Domain" in proc.stdout
