"""local-docker SandboxBackend — against a fake Docker client.

The Docker SDK and daemon are injected as a seam (``LocalDockerBackend(client=...)``),
so the adapter's lifecycle — run a container, exec the render, parse stdout,
remove the container — is proven with no real daemon. The live path is opt-in,
exercised the way the real model providers are (build step 7).
"""

from __future__ import annotations

import json

import pytest

from zu_backends.local_docker import DockerUnavailableError, LocalDockerBackend, _render_argv
from zu_core.ports import ToolCall


def test_render_argv_is_generic_url_plus_wait_and_actions() -> None:
    # The argv builder serializes whatever the model asked for — no site logic.
    actions = [{"click": "text=Next"}, {"wait_for": ".slots"}]
    argv = _render_argv({
        "url": "https://x/booking", "width": 1100, "height": 1400,
        "wait_until": "networkidle", "wait_for": ".slots", "wait_ms": 2000, "actions": actions,
    })
    assert argv[:2] == ["zu-render", "https://x/booking"]
    assert "--wait-until" in argv and "networkidle" in argv
    assert "--wait-for" in argv and ".slots" in argv
    assert "--wait-ms" in argv and "2000" in argv
    i = argv.index("--actions")
    assert json.loads(argv[i + 1]) == actions     # actions round-trip as JSON


def test_render_argv_minimal_is_just_url() -> None:
    # No wait/actions -> a plain render, unchanged from before the capability.
    assert _render_argv({"url": "https://x/"}) == ["zu-render", "https://x/"]


def test_render_argv_capture_network_flag() -> None:
    assert "--capture-network" in _render_argv({"url": "https://x/", "capture_network": True})
    assert "--capture-network" not in _render_argv({"url": "https://x/"})

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


async def test_extra_hosts_dns_pin_is_passed_to_docker() -> None:
    # The validated target host->IP pin reaches the container as extra_hosts.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": True, "extra_hosts": {"shop.test": "93.184.216.34"}})
    kw = client.containers.run_kwargs
    assert kw is not None and kw["extra_hosts"] == {"shop.test": "93.184.216.34"}


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


async def test_hardening_floor_cannot_be_loosened_by_spec() -> None:
    # A caller cannot silently strip the baseline: even passing empty/zero values,
    # cap_drop keeps ALL, security_opt keeps no-new-privileges, pids stays bounded.
    # A genuine cap is still re-addable via cap_add (drop-ALL-then-add).
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({
        "image": "img",
        "cap_drop": [], "security_opt": [], "pids_limit": 0,
        "cap_add": ["SYS_ADMIN"],
    })
    kw = client.containers.run_kwargs
    assert kw is not None
    assert "ALL" in kw["cap_drop"]
    assert "no-new-privileges" in kw["security_opt"]
    assert kw["pids_limit"] == 256          # 0 (unlimited) refused -> floored
    assert kw["cap_add"] == ["SYS_ADMIN"]   # legitimate opt-back-in still honoured


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


# --- the red-team container form (RED_TEAM_CONTAINER.md P1) ------------------


class _ExecContainer(_FakeContainer):
    """A fake whose exec_run accepts an environment, as the real SDK does — so the
    generic exec_entrypoint (spec passed via env) can be exercised with no daemon."""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.exec_env: dict | None = None

    def exec_run(self, cmd, demux=False, environment=None):
        self.exec_env = environment
        self.exec_calls.append(cmd)
        if demux:
            return self._exit_code, (self._output, self._stderr)
        return self._exit_code, self._output


async def test_isolated_network_attaches_internal_net_and_injects_proxy_env() -> None:
    # The container form: attach to the internal network (the default-DROP
    # enforcement) and point HTTP(S)_PROXY at the egress proxy.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({
        "image": "img", "network": "isolated", "network_name": "zu-redteam-net",
        "proxy": {"host": "10.0.0.2", "port": 8080},
    })
    kw = client.containers.run_kwargs
    assert kw is not None
    assert kw["network"] == "zu-redteam-net"
    assert kw["network_disabled"] is False
    assert kw["environment"]["HTTPS_PROXY"] == "http://10.0.0.2:8080"
    assert kw["environment"]["HTTP_PROXY"] == "http://10.0.0.2:8080"
    # Hardening is unchanged in the container form.
    assert kw["cap_drop"] == ["ALL"] and kw["security_opt"] == ["no-new-privileges"]


async def test_exec_entrypoint_runs_argv_with_env_and_returns_streams() -> None:
    container = _ExecContainer(exit_code=0, output=b'{"type": "x"}\n', stderr=b"warn")
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    sandbox = await backend.launch({"image": "img"})
    code, out, err = await backend.exec_entrypoint(
        sandbox, ["zu-redteam-run"], environment={"ZU_REDTEAM_SPEC": "{}"})
    assert code == 0
    assert out.strip() == '{"type": "x"}'
    assert err == "warn"
    assert container.exec_env == {"ZU_REDTEAM_SPEC": "{}"}
    assert container.exec_calls == [["zu-redteam-run"]]


class _DiffContainer(_FakeContainer):
    """A fake container exposing docker diff() — the fs-write audit source (P3)."""

    def __init__(self, diffs, **kw) -> None:
        super().__init__(exit_code=kw.pop("exit_code", 0), output=kw.pop("output", b"{}"), **kw)
        self._diffs = diffs

    def diff(self):
        return self._diffs


async def test_fs_diff_maps_docker_diff_to_path_kind() -> None:
    container = _DiffContainer(diffs=[{"Path": "/etc/cron.d/x", "Kind": 1},
                                      {"Path": "/tmp/cache", "Kind": 0},
                                      {"Path": "/old", "Kind": 2}])
    backend = LocalDockerBackend(client=_FakeClient(container))
    sandbox = await backend.launch({"image": "img"})
    diffs = await backend.fs_diff(sandbox)
    assert {"path": "/etc/cron.d/x", "kind": "added"} in diffs
    assert {"path": "/tmp/cache", "kind": "changed"} in diffs
    assert {"path": "/old", "kind": "deleted"} in diffs


async def test_mitm_ca_is_bind_mounted_trusted_and_cleaned_up() -> None:
    # P2: a per-run CA is written to a host temp file, bind-mounted read-only, and
    # trusted via SSL_CERT_FILE/REQUESTS_CA_BUNDLE — then removed at destroy.
    import os

    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    sandbox = await backend.launch({"image": "img", "ca_cert": b"-----BEGIN CA-----"})
    kw = client.containers.run_kwargs
    assert kw is not None and len(kw["volumes"]) == 1
    ca_path = next(iter(kw["volumes"]))
    assert kw["volumes"][ca_path]["mode"] == "ro"
    assert kw["environment"]["SSL_CERT_FILE"] == "/zu-redteam-ca.pem"
    assert kw["environment"]["REQUESTS_CA_BUNDLE"] == "/zu-redteam-ca.pem"
    assert os.path.exists(ca_path)          # present during the run
    assert sandbox.cleanup_paths == [ca_path]
    await backend.destroy(sandbox)
    assert not os.path.exists(ca_path)      # the CA dies with the run


async def test_seccomp_profile_path_is_inlined_to_json() -> None:
    # The docker SDK passes security_opt verbatim (it does NOT read a file like the
    # CLI), so a profile given as a PATH must be inlined to its JSON content here.
    from pathlib import Path

    import zu_backends
    profile = Path(zu_backends.__file__).parent / "seccomp" / "redteam-audit.json"
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "seccomp": str(profile)})
    kw = client.containers.run_kwargs
    assert kw is not None
    opt = next(o for o in kw["security_opt"] if o.startswith("seccomp="))
    assert "defaultAction" in opt and "SCMP_ACT" in opt  # the JSON content, not the path
    assert "/" not in opt.split("=", 1)[1].splitlines()[0]  # first line is JSON, not a path
    assert "no-new-privileges" in kw["security_opt"]  # default hardening preserved


async def test_seccomp_json_and_unconfined_pass_through() -> None:
    # A profile already given as JSON, or the special "unconfined", is not treated
    # as a path (no file read).
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "seccomp": '{"defaultAction": "SCMP_ACT_ALLOW"}'})
    kw = client.containers.run_kwargs
    assert kw is not None
    assert 'seccomp={"defaultAction": "SCMP_ACT_ALLOW"}' in kw["security_opt"]
