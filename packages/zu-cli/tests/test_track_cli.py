"""`zu run` record → replay: the deterministic track, end-to-end through the CLI.

The first run has no track, so the (scripted) model drives and every tool call it
makes is recorded to ``track.json`` beside the agent. The second run finds a
matching track and REPLAYS it — the navigator drives the recorded tool calls with
no model move for them; the model only reappears at the frontier to give the final
answer. ``--no-track`` opts out of both.

Hermetic: a bundle tool records each invocation to a sidecar file (so we can prove
the tool ran during replay) and the scripted provider stands in for the model. No
network, no key, no Docker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from zu_cli.main import app

runner = CliRunner()

_QUERY = "do the thing"

# A bundle tool that appends a line to the file named by $ZU_TEST_CALLS on every
# call — a witness that the tool actually executed, model-driven or replayed.
_TOOL_PY = (
    "import os\n"
    "class CallTool:\n"
    "    name = 'call_tool'\n"
    "    tier = 1\n"
    "    schema = {'name': 'call_tool', 'parameters': {'type': 'object', 'properties': {}}}\n"
    "    prompt_fragment = 'call_tool(): records a call'\n"
    "    capabilities = frozenset()\n"
    "    egress = frozenset()\n"
    "    async def __call__(self, ctx):\n"
    "        path = os.environ.get('ZU_TEST_CALLS')\n"
    "        if path:\n"
    "            with open(path, 'a', encoding='utf-8') as fh:\n"
    "                fh.write('x\\n')\n"
    "        return {'text': 'called'}\n"
)


def _write_bundle(tmp_path: Path, *, script: str) -> Path:
    """A bundle dir: its own call_tool + an agent.yaml with a scripted provider."""
    (tmp_path / "tools").mkdir(exist_ok=True)
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tools" / "calls.py").write_text(_TOOL_PY, encoding="utf-8")
    (tmp_path / "agent.yaml").write_text(
        f"provider: {{name: scripted, script: {script}}}\n"
        "plugins: {validators: []}\n"
        'tiers: {1: ["tools.calls:CallTool"]}\n'
        f'task: {{query: "{_QUERY}"}}\n',
        encoding="utf-8",
    )
    return tmp_path


def _isolate_tools_pkg() -> None:
    # The generic `tools` package is one-per-process in real use; pytest shares a
    # process, so drop any cached bundle module between loads.
    for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
        del sys.modules[m]


def _count(calls_file: Path) -> int:
    return len(calls_file.read_text(encoding="utf-8").splitlines()) if calls_file.exists() else 0


def test_run_records_then_replays_the_track(tmp_path, monkeypatch):
    calls = tmp_path / "calls.log"
    monkeypatch.setenv("ZU_TEST_CALLS", str(calls))

    # Run 1: no track. The model (scripted) calls the tool, then answers. The path
    # is recorded.
    _write_bundle(tmp_path, script="[{tool: call_tool}, {text: done, finish: stop}]")
    _isolate_tools_pkg()
    r1 = runner.invoke(app, ["run", str(tmp_path)])
    assert r1.exit_code == 0, r1.output
    assert "recorded 1 steps" in r1.output
    track_path = tmp_path / "track.json"
    assert track_path.exists()
    data = json.loads(track_path.read_text(encoding="utf-8"))
    assert data["task"] == _QUERY
    assert [s["tool"] for s in data["steps"]] == ["call_tool"]
    assert _count(calls) == 1  # model drove the one tool call

    # Run 2: a matching track exists. The navigator replays the tool call (no model
    # move for it); the model only answers at the frontier — so its script is just
    # the final answer, yet the tool still runs (via replay).
    calls.unlink()
    _write_bundle(tmp_path, script="[{text: done, finish: stop}]")
    _isolate_tools_pkg()
    r2 = runner.invoke(app, ["run", str(tmp_path)])
    assert r2.exit_code == 0, r2.output
    assert "replaying 1 recorded steps" in r2.output
    assert _count(calls) == 1  # the tool ran deterministically, from the track


def test_run_writes_cost_telemetry_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ZU_TEST_CALLS", str(tmp_path / "calls.log"))
    # the scripted model carries usage so the cost projection is non-trivial
    _write_bundle(
        tmp_path,
        script="[{tool: call_tool, usage: {input_tokens: 100, output_tokens: 20}}, "
               "{text: done, finish: stop, usage: {input_tokens: 50, output_tokens: 10}}]",
    )
    _isolate_tools_pkg()
    r = runner.invoke(app, ["run", str(tmp_path), "--no-track"])
    assert r.exit_code == 0, r.output
    assert "cost   :" in r.output and "tokens" in r.output
    # the per-agent ledger is appended with this run's telemetry
    ledger = tmp_path / "cost.jsonl"
    assert ledger.exists()
    entry = json.loads(ledger.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["model_calls"] == 2
    assert entry["total_tokens"] == 180          # 100+20+50+10
    assert entry["status"] == "success"


def test_no_track_neither_reads_nor_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("ZU_TEST_CALLS", str(tmp_path / "calls.log"))
    _write_bundle(tmp_path, script="[{tool: call_tool}, {text: done, finish: stop}]")
    _isolate_tools_pkg()
    r = runner.invoke(app, ["run", str(tmp_path), "--no-track"])
    assert r.exit_code == 0, r.output
    assert "track  :" not in r.output  # neither the replay nor the record line
    assert not (tmp_path / "track.json").exists()
