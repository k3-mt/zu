"""`zu run --every` — the scheduled-worker mode, and its duration parsing."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zu_cli.config import ConfigError
from zu_cli.main import _parse_duration, app

runner = CliRunner()


@pytest.mark.parametrize(
    "text,seconds",
    [("30s", 30), ("5m", 300), ("2h", 7200), ("1d", 86400), ("90", 90), ("1.5m", 90)],
)
def test_parse_duration(text, seconds):
    assert _parse_duration(text) == seconds


@pytest.mark.parametrize("bad", ["", "abc", "5x", "-1m", "0s"])
def test_parse_duration_rejects_bad(bad):
    with pytest.raises(ConfigError):
        _parse_duration(bad)


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_every_with_max_runs_stops(tmp_path):
    # A scheduled run that stops after N iterations (so it's testable) and does
    # not sleep meaningfully between them.
    answer = json.dumps({"name": "Acme", "price": "$9"})
    cfg = _write(
        tmp_path,
        "zu.yaml",
        "provider:\n  name: scripted\n"
        f"  script: [{{ text: '{answer}', finish: stop }}]\n"
        "plugins:\n  validators: [schema]\n",
    )
    task = _write(
        tmp_path,
        "task.yaml",
        "query: extract\noutput_schema:\n  type: object\n"
        "  properties: { name: { type: string }, price: { type: string } }\n"
        "  required: [name, price]\n",
    )
    result = runner.invoke(
        app, ["run", task, "--config", cfg, "--every", "0.01s", "--max-runs", "2"]
    )
    assert result.exit_code == 0, result.output
    assert "--- run 1 ---" in result.output
    assert "--- run 2 ---" in result.output
    assert "--- run 3 ---" not in result.output
