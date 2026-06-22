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


def test_entrypoint_loads_a_bundles_tools_from_zu_bundle(tmp_path, monkeypatch, capsys) -> None:
    # The in-container half of bundle support: ZU_BUNDLE points at the mounted
    # bundle; the entrypoint adds it to sys.path so the agent's `tools.x:Class`
    # import-ref resolves and the custom tool runs — no packaging, no install.
    import sys

    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tools" / "greet.py").write_text(
        "class Greet:\n"
        "    name = 'greet'\n"
        "    tier = 1\n"
        "    schema = {'name': 'greet', 'parameters': {'type': 'object',\n"
        "              'properties': {'name': {'type': 'string'}}, 'required': ['name']}}\n"
        "    prompt_fragment = 'greet(name)'\n"
        "    capabilities = frozenset()\n"
        "    egress = frozenset()\n"
        "    async def __call__(self, ctx, name):\n"
        "        return {'text': 'hi ' + name}\n",
        encoding="utf-8",
    )
    config = {
        "provider": {"name": "scripted", "script": [
            {"tool": "greet", "args": {"name": "World"}},
            {"text": '{"ok": true}', "finish": "stop"}]},
        "tiers": {1: ["tools.greet:Greet"]},
        "plugins": {"validators": []},
    }
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.setenv("ZU_BUNDLE", str(tmp_path))
    monkeypatch.setenv("ZU_TASK", json.dumps({"query": "q"}))
    monkeypatch.setenv("ZU_CONFIG", json.dumps(config))

    for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
        del sys.modules[m]
    try:
        rc = run_contained_from_env()
        payload = json.loads(capsys.readouterr().out.strip())
        assert rc == 0 and payload["result"]["status"] == "success"
        # the custom tool actually ran inside
        assert any(e["type"] == "harness.tool.invoked" for e in payload["events"])
    finally:
        for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            del sys.modules[m]


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


@pytest.mark.docker
def test_zu_pack_builds_a_runnable_bundle_image() -> None:
    # `zu pack` bakes the bundle (its tools/, and requirements if any) into a
    # standalone image FROM the base; running it executes the agent with the
    # bundle's tools baked in.
    import subprocess
    from pathlib import Path

    from zu_cli import deploy

    bundle = Path(__file__).resolve().parent / "agents" / "custom-tool"
    base = os.environ.get("ZU_SANDBOX_IMAGE", "zu:test")
    tag = "zu-custom-tool:packtest"
    df = deploy.pack_dockerfile_text(base)
    assert subprocess.run(deploy.pack_build_command(tag, str(bundle)),
                          input=df.encode()).returncode == 0
    try:
        out = subprocess.run(["docker", "run", "--rm", tag],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        assert "status : success" in out.stdout
        assert "greeting" in out.stdout       # the baked custom tool ran
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@pytest.mark.docker
async def test_launcher_mounts_a_bundles_own_tools() -> None:
    # A bundle's custom tools/ are not in the image — mounting the bundle dir lets
    # its `tools.x:Class` import-refs resolve INSIDE the contained container.
    from pathlib import Path

    from zu_backends.local_docker import LocalDockerBackend
    from zu_cli.sandbox import SandboxLauncher

    bundle = Path(__file__).resolve().parent / "agents" / "custom-tool"
    image = os.environ.get("ZU_SANDBOX_IMAGE", "zu:test")
    config = {
        "provider": {"name": "scripted", "script": [
            {"tool": "greet", "args": {"name": "World"}},
            {"text": '{"greeting": "Hello, World!"}', "finish": "stop"}]},
        "tiers": {1: ["tools.greet:Greet"]},
        "plugins": {"validators": []},
    }
    launcher = SandboxLauncher(backend=LocalDockerBackend(), image=image)
    result, events = await launcher.run(
        {"query": "greet"}, config, allowlist=[], bundle_dir=str(bundle),
    )
    assert result.status.value == "success"
    assert result.value == {"greeting": "Hello, World!"}
    # the bundle's own tool ran inside the box
    assert any(e.get("type") == "harness.tool.invoked" for e in events)
