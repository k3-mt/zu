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

import pytest
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


# --- the host-side launcher (wiring verified with a fake; Docker is the gated seam) -------


class _RecordingLauncher:
    """A stand-in for SandboxLauncher: records the run_entrypoint call and returns a canned
    payload, so the construction launcher's WIRING is verified with no Docker, at $0 (the
    real container/Docker is exercised only by the @pytest.mark.docker test below)."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    async def run_entrypoint(self, entrypoint, exec_env, *, allowlist, bundle_dir=None) -> dict:
        self.calls.append({"entrypoint": entrypoint, "exec_env": exec_env,
                           "allowlist": allowlist, "bundle_dir": bundle_dir})
        return self._payload


async def test_launch_contained_construction_wires_the_entrypoint(tmp_path):
    from zu_cli.construct_sandbox import launch_contained_construction

    launcher = _RecordingLauncher(
        {"ok": True, "converged": True, "ready": True, "track": "{}",
         "rounds": [], "violations": []})
    report = await launch_contained_construction(
        launcher, str(tmp_path / "agent"), allowlist=["api.anthropic.com"],
        max_rounds=2, min_resilience=0.9)

    assert report["ready"] and report["track"] == "{}"
    call = launcher.calls[0]
    assert call["entrypoint"] == ["zu-construct-contained"]   # the construction entrypoint
    assert call["exec_env"] == {"ZU_CONSTRUCT_MAX_ROUNDS": "2",
                                "ZU_CONSTRUCT_MIN_RESILIENCE": "0.9"}
    assert call["allowlist"] == ["api.anthropic.com"]          # egress only to the model
    assert call["bundle_dir"].endswith("agent")               # the agent mounted at /bundle


def test_model_egress_derivation():
    from types import SimpleNamespace

    from zu_cli.main import _model_egress

    def _cfg(provider):
        return SimpleNamespace(provider=provider)

    # A scripted/offline brain needs no egress.
    assert _model_egress(_cfg(SimpleNamespace(name="scripted"))) == []
    # An explicit base_url → just its host.
    assert _model_egress(_cfg(SimpleNamespace(
        name="openai-compatible", base_url="https://openrouter.ai/api/v1", base_url_env=None,
    ))) == ["openrouter.ai"]
    # A built-in provider with no base_url → its known default host.
    assert _model_egress(_cfg(SimpleNamespace(
        name="anthropic", base_url=None, base_url_env=None))) == ["api.anthropic.com"]


@pytest.mark.docker
async def test_contained_construction_runs_in_the_box(tmp_path) -> None:
    # The real launcher end-to-end: drives Docker (internal net + proxy sidecar, caps
    # dropped, blocking seccomp), mounts the agent ro at /bundle, execs
    # zu-construct-contained in the box, and parses the report back across the boundary.
    # The brain is SCRIPTED, so it converges with NO model call and allowlist=[] (egress
    # denied) — the contained mechanics are verified at $0. Needs the rebuilt image; gated
    # to --run-docker.
    import os

    from zu_backends.local_docker import LocalDockerBackend
    from zu_cli.construct_sandbox import launch_contained_construction
    from zu_cli.sandbox import SandboxLauncher

    d = _scripted_brain_agent(tmp_path)
    image = os.environ.get("ZU_SANDBOX_IMAGE", "zu:test")
    launcher = SandboxLauncher(backend=LocalDockerBackend(), image=image)
    report = await launch_contained_construction(
        launcher, str(d), allowlist=[], max_rounds=2)
    assert report["ok"] is True and report["converged"] is True
    assert report["track"]  # the hardened track came back from inside the box
