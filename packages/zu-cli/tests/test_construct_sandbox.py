"""The in-container construction entrypoint — autonomous construct() run contained.

Exercised entirely OFFLINE at ~$0: the brain is a ScriptedProvider (the agent's `provider:`
swapped to `scripted`), so LiveStrategist's model calls replay canned moves and make NO API
call — and the offline spine replays the captured bundle. No Docker, no key, no network, no
spend (the container/Docker is the launcher's un-fakeable seam, tested separately).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

from zu_cli.construct_sandbox import construct_contained_from_env, run_contained_construction

_BROWSER_WIDGET = Path(__file__).resolve().parents[3] / "examples" / "agents" / "browser-widget"


def _scripted_brain_agent(tmp_path, fix: str = '{"fixes": [{"step": 0, "near": "price"}]}') -> Path:
    """A copy of the browser-widget example whose BRAIN provider is scripted to return ``fix``
    — so construction runs with zero live model calls. Returns the agent dir."""
    d = tmp_path / "agent"
    shutil.copytree(_BROWSER_WIDGET, d, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))
    doc = yaml.safe_load((d / "agent.yaml").read_text(encoding="utf-8"))
    doc["provider"] = {"name": "scripted", "script": [{"text": fix, "finish": "stop"}]}
    (d / "agent.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return d


def test_contained_construction_converges_offline(tmp_path):
    # The example trips G1; the scripted brain returns the `near` fix, so construction
    # converges — proven with no live model and no Docker.
    d = _scripted_brain_agent(tmp_path)
    payload = run_contained_construction(str(d))

    assert payload["ok"] and payload["converged"] and payload["ready"]
    assert payload["track"]  # the hardened track.json came back for review
    json.loads(payload["track"])  # ...and it is valid JSON
    # The source agent is untouched — construction worked on a writable copy (ro-mount safe).
    assert not (d / "track.json").exists()


def test_contained_construction_reports_when_unfixed(tmp_path):
    # A brain that returns no usable fix → the loop gives up; the report carries the
    # standing violations (for review) and no track.
    d = _scripted_brain_agent(tmp_path, fix="sorry, no idea")
    payload = run_contained_construction(str(d), max_rounds=2)

    assert payload["ok"] and not payload["converged"]
    assert any(v["rule"] == "single-selector" for v in payload["violations"])
    assert payload["track"] is None


def test_entrypoint_reads_env_and_emits_json(tmp_path, capsys, monkeypatch):
    # The console-script wrapper: reads the mounted agent at ZU_BUNDLE and writes one JSON
    # object on stdout (the launcher's parse contract).
    d = _scripted_brain_agent(tmp_path)
    monkeypatch.setenv("ZU_BUNDLE", str(d))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # belt-and-braces: never go live

    rc = construct_contained_from_env()

    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] and payload["converged"] and payload["ready"]


def test_entrypoint_without_bundle_errors(capsys, monkeypatch):
    monkeypatch.delenv("ZU_BUNDLE", raising=False)
    rc = construct_contained_from_env()
    assert rc == 1
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["ok"] is False and "ZU_BUNDLE" in payload["error"]
