"""local-docker SandboxBackend — against a fake Docker client.

The Docker SDK and daemon are injected as a seam (``LocalDockerBackend(client=...)``),
so the adapter's lifecycle — run a container, exec the render, parse stdout,
remove the container — is proven with no real daemon. The live path is opt-in,
exercised the way the real model providers are (build step 7).
"""

from __future__ import annotations

import json

import pytest

from zu_backends.local_docker import DockerUnavailableError, LocalDockerBackend
from zu_core.ports import ToolCall

_RENDERED = "<html><body><h1>Rendered</h1></body></html>"


class _FakeContainer:
    def __init__(self, exit_code: int, output: bytes, status: str = "running",
                 stderr: bytes = b"") -> None:
        self._exit_code = exit_code
        self._output = output
        self._stderr = stderr
        self.removed = False
        self.exec_calls: list[list[str]] = []
        self.id = "fake-container-id"
        self.status = status
        self.reloads = 0

    def reload(self) -> None:
        self.reloads += 1

    def exec_run(self, cmd, demux=False):
        # Mirror the real Docker SDK: demux=True returns (stdout, stderr) so the
        # backend can read stdout in isolation from Chromium's noisy stderr.
        self.exec_calls.append(cmd)
        if demux:
            return self._exit_code, (self._output, self._stderr)
        return self._exit_code, self._output

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self, container: _FakeContainer) -> None:
        self._container = container
        self.run_kwargs: dict | None = None

    def run(self, image, **kwargs):
        self.run_kwargs = {"image": image, **kwargs}
        return self._container


class _FakeClient:
    def __init__(self, container: _FakeContainer) -> None:
        self.containers = _FakeContainers(container)


async def _render(container: _FakeContainer, url: str = "http://spa.test/") -> dict:
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    sandbox = await backend.launch({"image": "zu/render-chromium:latest", "tier": 2})
    try:
        return await backend.exec(sandbox, ToolCall(name="render_dom", args={"url": url}))
    finally:
        await backend.destroy(sandbox)


async def test_lifecycle_parses_json_stdout_and_removes_container() -> None:
    payload = json.dumps({"status": 200, "html": _RENDERED, "url": "http://spa.test/"}).encode()
    container = _FakeContainer(exit_code=0, output=payload)
    obs = await _render(container)

    assert obs["html"] == _RENDERED
    assert obs["status"] == 200
    assert container.removed is True  # destroyed even on the happy path
    assert container.exec_calls == [["zu-render", "http://spa.test/"]]


async def test_network_disabled_by_default() -> None:
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img"})
    assert client.containers.run_kwargs is not None
    assert client.containers.run_kwargs["network_disabled"] is True


async def test_network_enabled_when_requested() -> None:
    # render_dom launches with network=True so the browser can fetch the page;
    # the backend must translate that into an un-disabled container network.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": True})
    assert client.containers.run_kwargs is not None
    assert client.containers.run_kwargs["network_disabled"] is False


async def test_container_is_hardened_by_default() -> None:
    # The tier-2 container runs an untrusted, model-chosen URL: it must drop all
    # caps, forbid privilege escalation, and bound pids by default.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img"})
    kw = client.containers.run_kwargs
    assert kw is not None
    assert kw["cap_drop"] == ["ALL"]
    assert kw["security_opt"] == ["no-new-privileges"]
    assert kw["pids_limit"] == 256
    assert container.reloads >= 1  # readiness was actually awaited


async def test_dead_container_fails_fast() -> None:
    # If the container exits before becoming ready, launch raises rather than
    # execing into a dead container.
    container = _FakeContainer(exit_code=0, output=b"", status="exited")
    backend = LocalDockerBackend(client=_FakeClient(container), startup_timeout_s=1)
    with pytest.raises(DockerUnavailableError):
        await backend.launch({"image": "img"})


async def test_nonzero_exit_becomes_error_observation() -> None:
    container = _FakeContainer(exit_code=1, output=b"boom")
    obs = await _render(container)
    assert obs["status"] == 500
    assert "render failed" in obs["error"]
    assert container.removed is True


async def test_non_json_stdout_becomes_error_observation() -> None:
    # The entrypoint contract is a JSON line; non-JSON stdout is a broken render,
    # not a page. It must surface as an ERROR observation (so the `error`
    # detector fires) rather than be laundered into a fake 200 page — but the raw
    # stdout is preserved for debugging.
    container = _FakeContainer(exit_code=0, output=_RENDERED.encode())
    obs = await _render(container)
    assert obs["status"] == 500
    assert obs["error"] == "render produced non-JSON output"
    assert obs["raw"] == _RENDERED
    assert "html" not in obs  # never presented as page content
    assert container.removed is True


async def test_noisy_stderr_does_not_corrupt_json_stdout() -> None:
    # Regression: Chromium under --no-sandbox spams stderr (GPU/font/dbus
    # warnings). With demux those must NOT bleed into the JSON line on stdout,
    # or every real render would be misread as "non-JSON output". The backend
    # parses stdout alone; stderr is ignored on success.
    payload = json.dumps({"status": 200, "html": _RENDERED, "url": "http://spa.test/"}).encode()
    noisy = b"[WARNING] dbus: failed\nlibGL error: MESA-LOADER\n"
    container = _FakeContainer(exit_code=0, output=payload, stderr=noisy)
    obs = await _render(container)
    assert obs["html"] == _RENDERED and obs["status"] == 200


async def test_viewport_args_are_passed_to_the_entrypoint() -> None:
    # A width/height in the tool call reaches zu-render as argv flags.
    container = _FakeContainer(exit_code=0, output=b'{"status":200,"html":"","url":"http://spa.test/"}')
    backend = LocalDockerBackend(client=_FakeClient(container))
    sandbox = await backend.launch({"image": "img", "tier": 2})
    await backend.exec(sandbox, ToolCall(
        name="render_dom", args={"url": "http://spa.test/", "width": 375, "height": 812}))
    assert container.exec_calls == [["zu-render", "http://spa.test/", "--width", "375", "--height", "812"]]


async def test_missing_docker_sdk_raises_clear_error() -> None:
    # No client injected and no Docker SDK importable -> a clear, actionable
    # error, not an opaque ImportError deep in a render.
    backend = LocalDockerBackend(client=None)
    import builtins

    real_import = builtins.__import__

    def no_docker(name, *args, **kwargs):
        if name == "docker":
            raise ImportError("no docker")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = no_docker
    try:
        with pytest.raises(DockerUnavailableError):
            await backend.launch({"image": "img"})
    finally:
        builtins.__import__ = real_import
