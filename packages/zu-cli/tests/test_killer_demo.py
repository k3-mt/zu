"""The killer demo — shipped in the package (`zu demo`) and ready straight away.

Proves the demo runs the full three-pillar arc offline with zero setup (no key,
no network, no Docker), exits cleanly via the `zu demo` command, and that the
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
async def test_arc_runs_to_a_grounded_success():
    result, bus, backend = await demo.run_arc(demo.scripted_arc())

    # Pillar 3: schema-valid + grounded (both validators registered).
    assert result.status is Status.SUCCESS
    assert result.value == {"name": "Acme Widget", "price": "$9.00"}

    events = await bus.query()
    types = [e.type for e in events]

    # Pillar 1: a detector drove a tier climb 1 -> 2 (not a terminal give-up).
    assert "harness.detector.fired" in types
    escalated = [e for e in events if e.type == "harness.task.escalated"]
    assert len(escalated) == 1
    assert escalated[0].payload["reason"] == "js-shell"
    assert (escalated[0].payload["from_tier"], escalated[0].payload["to_tier"]) == (1, 2)
    assert "exhausted" not in escalated[0].payload

    # The tier-2 browser was leased and torn down.
    assert backend.launched and backend.destroyed == 1
    assert backend.launched[0]["tier"] == 2

    # Pillar 2: a queryable log ending in completion.
    assert types[-1] == "harness.task.completed"


def test_zu_demo_command_offline_exits_zero():
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0, result.output
    assert "ESCALATE 1→2" in result.output
    assert "RESULT   : success" in result.output
    assert "Acme Widget" in result.output


def test_zu_demo_real_provider_fails_cleanly_without_a_key():
    # Wiring reaches the real adapter; with no key it fails fast — reported, not
    # crashed — and the demo exits 1.
    result = runner.invoke(
        app,
        ["demo", "--provider", "anthropic", "--model", "claude-sonnet-4-6",
         "--api-key-env", "ZU_ABSENT_KEY"],
    )
    assert result.exit_code == 1
    assert "provider : anthropic" in result.output
    assert "run failed" in result.output


def test_example_wrapper_runs_as_a_subprocess():
    # The literal "clone the repo and run it" path stays identical to `zu demo`.
    demo_path = Path(__file__).resolve().parents[3] / "examples" / "killer_demo.py"
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(demo_path)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "ESCALATE 1→2" in proc.stdout
    assert "RESULT   : success" in proc.stdout
