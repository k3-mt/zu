"""The whole-agent-in-container launcher (zu_cli.sandbox).

The in-container entrypoint (``run_contained_from_env``) is pure and tested in
process. The launcher itself drives a real Docker daemon (internal network +
egress-proxy sidecar + target container), so it is marked ``@pytest.mark.docker``
and only runs with ``--run-docker``.

    docker build -t zu:test .
    ZU_SANDBOX_IMAGE=zu:test pytest --run-docker packages/zu-cli/tests/test_sandbox.py
"""

from __future__ import annotations

import json
import os

import pytest

from zu_cli.sandbox import run_contained_from_env

_SCRIPTED_CONFIG = {
    "provider": {"name": "scripted", "script": [{"text": '{"ok": true}', "finish": "stop"}]},
    "containment": "required",
}


def test_in_container_entrypoint_emits_result_json(monkeypatch, capsys) -> None:
    # Simulate being inside the box: the launcher would have set these.
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.setenv("ZU_TASK", json.dumps({"query": "q"}))
    monkeypatch.setenv("ZU_CONFIG", json.dumps(_SCRIPTED_CONFIG))

    rc = run_contained_from_env()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["result"]["status"] == "success"
    assert payload["result"]["value"] == {"ok": True}


@pytest.mark.docker
async def test_launcher_runs_the_agent_inside_the_box() -> None:
    from zu_backends.local_docker import LocalDockerBackend
    from zu_cli.sandbox import SandboxLauncher

    image = os.environ.get("ZU_SANDBOX_IMAGE", "zu:test")
    launcher = SandboxLauncher(backend=LocalDockerBackend(), image=image)
    result, events = await launcher.run(
        {"query": "q"}, _SCRIPTED_CONFIG, allowlist=["example.com"]
    )
    assert result.status.value == "success"
    assert result.value == {"ok": True}
    # Observability survives containment: the FULL in-container event lifecycle is
    # surfaced back across the boundary, not just the result. A contained run is
    # still lossless and replayable from the host's side.
    types = [e.get("type") for e in events]
    assert "harness.task.started" in types
    assert "harness.turn.started" in types
    assert "harness.turn.completed" in types
    assert "harness.task.completed" in types
