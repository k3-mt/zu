"""local-docker SandboxBackend — against a fake Docker client.

The Docker SDK and daemon are injected as a seam (``LocalDockerBackend(client=...)``),
so the adapter's lifecycle — run a container, exec the render, parse stdout,
remove the container — is proven with no real daemon. The live path is opt-in,
exercised the way the real model providers are (build step 7).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from zu_backends.local_docker import (
    DockerUnavailableError,
    ImagePolicyError,
    LocalDockerBackend,
    NetworkPolicyError,
    _image_is_digest_pinned,
    _image_registry,
    _render_argv,
    _spec_target,
)
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
    await backend.launch({"image": "img", "network": True, "allow_unrestricted_egress": True})
    assert client.containers.run_kwargs is not None
    assert client.containers.run_kwargs["network_disabled"] is False


async def test_extra_hosts_dns_pin_is_passed_to_docker() -> None:
    # The validated target host->IP pin reaches the container as extra_hosts.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": True, "allow_unrestricted_egress": True,
                          "extra_hosts": {"shop.test": "93.184.216.34"}})
    kw = client.containers.run_kwargs
    assert kw is not None and kw["extra_hosts"] == {"shop.test": "93.184.216.34"}


async def test_dns_gate_is_passed_to_docker() -> None:
    # ZU-NET-1: an EgressEnforcement's DNS gate reaches the container as `dns`, so
    # the embedded resolver cannot be used as a covert egress channel.
    container = _FakeContainer(exit_code=0, output=b"{}")
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch(
        {"image": "img", "network": "isolated", "network_name": "net",
         "extra_hosts": {"proxy": "10.0.0.5"}, "dns": ["127.0.0.1"]}
    )
    kw = client.containers.run_kwargs
    assert kw is not None and kw["dns"] == ["127.0.0.1"]


async def test_no_dns_key_leaves_docker_default() -> None:
    # Backward-compatible: omitting `dns` means the kwarg is not set at all.
    container = _FakeContainer(exit_code=0, output=b"{}")
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": True, "allow_unrestricted_egress": True})
    kw = client.containers.run_kwargs
    assert kw is not None and "dns" not in kw


# --- F34: image pull policy (registry allowlist + optional digest pin) ---------


def test_image_registry_parsing_is_lexical() -> None:
    # A bare name / ns/name is implicit docker.io (registry ""); a host-looking
    # first segment is the registry; a digest doesn't confuse the split.
    assert _image_registry("img") == ""
    assert _image_registry("library/nginx:latest") == ""
    assert _image_registry("zu/render-chromium:latest") == ""
    assert _image_registry("ghcr.io/k3-mt/zu-render:latest") == "ghcr.io"
    assert _image_registry("registry.example.com:5000/x/y@sha256:" + "a" * 64) \
        == "registry.example.com:5000"
    assert _image_registry("localhost/x") == "localhost"


def test_image_digest_pin_detection() -> None:
    assert _image_is_digest_pinned("x@sha256:" + "a" * 64) is True
    assert _image_is_digest_pinned("x:latest") is False


async def test_launch_refuses_image_from_registry_not_on_allowlist() -> None:
    # F34: the load-bearing guard — an arbitrary registry cannot be auto-pulled.
    # On OLD code (no policy) this image would be run without complaint.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    with pytest.raises(ImagePolicyError):
        await backend.launch({"image": "evil.example.com/malware:latest"})
    assert client.containers.run_kwargs is None  # never reached containers.run


async def test_launch_allows_default_and_localhost_registries() -> None:
    # Non-breaking floor: implicit docker.io (bare/ns names) and localhost pass.
    for image in ("img", "zu/render-chromium:latest", "localhost/x:latest"):
        container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
        client = _FakeClient(container)
        backend = LocalDockerBackend(client=client)
        await backend.launch({"image": image})
        assert client.containers.run_kwargs is not None


async def test_launch_allows_registry_via_spec_allowlist() -> None:
    # The allowlist is extensible per-launch (config-derived, not hardcoded).
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "ghcr.io/k3-mt/zu-render:latest",
                          "image_allowlist": ["ghcr.io"]})
    assert client.containers.run_kwargs is not None


async def test_launch_allows_registry_via_env_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZU_IMAGE_REGISTRY_ALLOWLIST", "ghcr.io, quay.io")
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "quay.io/org/x:latest"})
    assert client.containers.run_kwargs is not None


async def test_require_digest_refuses_a_floating_tag() -> None:
    # F34 digest-pin policy: with the flag set, a mutable :tag is refused; only an
    # @sha256: reference (one immutable image) is run.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    with pytest.raises(ImagePolicyError):
        await backend.launch({"image": "img:latest", "require_digest": True})
    assert client.containers.run_kwargs is None
    # A digest-pinned reference passes the same policy.
    digest = "img@sha256:" + "a" * 64
    await backend.launch({"image": digest, "require_digest": True})
    assert client.containers.run_kwargs is not None


# --- F38: unrestricted egress (network=True) must be an explicit opt-in ---------


async def test_network_true_without_opt_in_is_refused() -> None:
    # F38: on OLD code network=True silently disabled isolation with no proxy. Now
    # unrestricted egress requires an explicit, logged opt-in or it is refused.
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    with pytest.raises(NetworkPolicyError):
        await backend.launch({"image": "img", "network": True})
    assert client.containers.run_kwargs is None


async def test_network_true_with_opt_in_flag_launches_unrestricted() -> None:
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": True, "allow_unrestricted_egress": True})
    kw = client.containers.run_kwargs
    assert kw is not None and kw["network_disabled"] is False


async def test_network_true_opt_in_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZU_ALLOW_UNRESTRICTED_EGRESS", "1")
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": True})
    kw = client.containers.run_kwargs
    assert kw is not None and kw["network_disabled"] is False


async def test_isolated_network_needs_no_egress_opt_in() -> None:
    # The enforced-egress tier is unaffected: "isolated" never requires the opt-in
    # (its egress is proxy-scoped, not unrestricted).
    container = _FakeContainer(exit_code=0, output=b'{"html": ""}')
    client = _FakeClient(container)
    backend = LocalDockerBackend(client=client)
    await backend.launch({"image": "img", "network": "isolated", "network_name": "net"})
    kw = client.containers.run_kwargs
    assert kw is not None and kw["network"] == "net"


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


# --- F39: the generic backend names no specific consumer ----------------------


def test_generic_backend_has_no_site_specific_naming() -> None:
    # F39: the generic sandbox backend must not couple to a specific consumer.
    # Its source carries no red-team / MITM naming nor a RED_TEAM_CONTAINER.md
    # reference (behaviour is unchanged; this is a naming/doc de-coupling). The one
    # retained token is the compat default mount value, which is opaque.
    import zu_backends.local_docker as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    lowered = src.lower()
    assert "red-team" not in lowered and "red_team" not in lowered
    assert "redteam" not in lowered.replace("/zu-redteam-ca.pem", "")  # only the compat default
    assert "mitm" not in lowered
    assert "RED_TEAM_CONTAINER" not in src


# --- F40: the only CONNECT/dial path is the egress proxy's (resolve-then-pin) ---


def test_local_docker_has_no_independent_connect_dial_path() -> None:
    # F40 evidence: the MITM/CONNECT dial with resolve-then-pin (issue #50) lives
    # solely in egress_proxy (which resolves once via _resolve_validated_ip and
    # dials the pinned IP). The generic backend has NO socket/connect path of its
    # own — it injects proxy env + extra_hosts DNS pins and lets Docker route the
    # container through the proxy — so there is no separate re-resolving dial to fix.
    import zu_backends.local_docker as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    for needle in ("open_connection", "create_connection", "getaddrinfo",
                   ".connect(", "socket.socket"):
        assert needle not in src, f"unexpected dial primitive in local_docker: {needle}"


# --- the contained-egress container form --------------------------------------


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


@pytest.mark.docker
@pytest.mark.skipif(
    not os.environ.get("ZU_BROWSER_LIVE"),
    reason="launches the real Chromium render container + hits the network; GitHub-hosted "
    "runners can't reliably start headless Chromium in Docker. Set ZU_BROWSER_LIVE=1 on a "
    "runner that can (mirrors the redteam live-gate).",
)
async def test_browser_session_holds_state_across_commands_live() -> None:
    # The persistent session: open a page, then read it again — the SAME browser
    # is held across commands (state persists), proven against the real image.
    image = os.environ.get("ZU_RENDER_IMAGE", "ghcr.io/k3-mt/zu-render-chromium:latest")
    session = await LocalDockerBackend().open_session(
        {"image": image, "network": True, "allow_unrestricted_egress": True,
         "image_allowlist": ["ghcr.io"]})
    try:
        opened = await session.send({"op": "open", "url": "https://example.com"})
        assert opened["status"] == 200 and "Example Domain" in opened["text"]
        again = await session.send({"op": "read"})            # no re-navigation
        assert "Example Domain" in again["text"]              # the page is still held
    finally:
        await session.close()


@pytest.mark.docker
@pytest.mark.skipif(
    not os.environ.get("ZU_BROWSER_LIVE"),
    reason="launches real Chromium + CDP; set ZU_BROWSER_LIVE=1 on a runner that can. "
    "Proves the §4/§5 live ops (axtree/locate/pointer/screenshot) against real Chromium — "
    "the real AX-tree shape, real bounding boxes, and isTrusted input — not offline fakes.",
)
async def test_axtree_locate_pointer_screenshot_against_real_chromium_live() -> None:
    import base64

    image = os.environ.get("ZU_RENDER_IMAGE", "ghcr.io/k3-mt/zu-render-chromium:latest")
    session = await LocalDockerBackend().open_session(
        {"image": image, "network": True, "allow_unrestricted_egress": True,
         "image_allowlist": ["ghcr.io"]})
    try:
        # A page with a real, addressable button (so the AX tree exposes it).
        await session.send({"op": "open", "url": "data:text/html,"
                            "<title>T</title><button>Place order</button>"})

        # axtree: the real CDP getFullAXTree shape the harness normalises.
        ax = await session.send({"op": "axtree"})
        assert isinstance(ax.get("axtree"), list) and ax["axtree"]
        roles = [n.get("role", {}).get("value") for n in ax["axtree"] if isinstance(n, dict)]
        assert "button" in roles                         # the button is in the real AX tree

        # locate: the real bounding box of the button + the current cursor.
        loc = await session.send({"op": "locate",
                                  "locator": {"role": "button", "name": "Place order"}})
        assert isinstance(loc.get("bounds"), list) and len(loc["bounds"]) == 4
        assert loc["bounds"][2] > 0 and loc["bounds"][3] > 0   # a real, non-zero box
        assert loc.get("cursor") == [0.0, 0.0]                 # initial cursor

        # pointer: stream trusted moves + a click; the cursor updates to the dest.
        x = loc["bounds"][0] + loc["bounds"][2] / 2
        y = loc["bounds"][1] + loc["bounds"][3] / 2
        ptr = await session.send({"op": "pointer", "click": True,
                                  "samples": [{"x": x, "y": y, "dt": 0.0}]})
        assert ptr.get("dispatched") == 1 and ptr.get("clicked") is True
        assert ptr["cursor"] == [x, y]                         # the next locate sees it

        loc2 = await session.send({"op": "locate",
                                   "locator": {"role": "button", "name": "Place order"}})
        assert loc2["cursor"] == [x, y]                        # cursor really moved

        # screenshot: a real, decodable PNG.
        shot = await session.send({"op": "screenshot"})
        png = base64.b64decode(shot["screenshot_b64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and shot["mime"] == "image/png"
    finally:
        await session.close()


@pytest.mark.docker
@pytest.mark.skipif(
    not os.environ.get("ZU_BROWSER_LIVE"),
    reason="launches real Chromium + CDP; set ZU_BROWSER_LIVE=1. Proves the END-TO-END "
    "PRODUCTION cross-tool path against REAL Chromium: a run's shared live session lives "
    "in the module registry, and the REAL PointerControl (NO injected backend/session) "
    "ATTACHES to it and clicks a real button by an OPAQUE HANDLE the harness resolves "
    "from the shared handle_map — the model never supplies a selector.",
)
async def test_real_pointer_attaches_to_run_session_and_clicks_by_handle_live() -> None:
    # The cross-tool sharing seam is the module registry (a per-tool backend shares
    # NOTHING). We open one real session, register it as the run's shared session and
    # populate the run's handle_map exactly as action_surface(op=open) does — then drive
    # the REAL PointerControl with NO injection: it must ATTACH via the registry and
    # resolve the opaque handle harness-side. (A data: URL is used at the CONTAINER
    # boundary — the tool's SSRF guard only allows http/https, so the session, not the
    # tool, opens the test page; the cross-tool REGISTRY path is what this proves live.)
    from zu_tools import _session
    from zu_tools.pointer import PointerControl

    image = os.environ.get("ZU_RENDER_IMAGE", "ghcr.io/k3-mt/zu-render-chromium:latest")
    run = "live-run-share"

    class _Ctx:
        spec = type("S", (), {"task_id": run})()

    backend = LocalDockerBackend()
    with _session._LOCK:
        _session._RUNS.pop(run, None)
    session = await backend.open_run_session(
        {"image": image, "network": True, "allow_unrestricted_egress": True,
         "image_allowlist": ["ghcr.io"]}, run_key=run)
    try:
        await session.send({"op": "open", "url": "data:text/html,"
                           "<title>T</title><button>Place order</button>"})
        # Register the shared session + handle_map the way action_surface would.
        with _session._LOCK:
            _session._RUNS[run] = _session._RunEntry(handle=session)
        _session.put_handle_map(run, {"a1": {"role": "button", "name": "Place order"}})

        # The REAL pointer, no injection: ATTACH + resolve the opaque handle harness-side.
        out = await PointerControl(seed="live")(_Ctx(), op="move_click", handle="a1")
        assert out["pointer"]["clicked"] is True
        assert out["pointer"]["samples"] > 0
        assert "stale_handle" not in out  # it found and clicked the real button
    finally:
        await _session.close_run(run)  # authoritative run-end teardown
    assert run not in _session._RUNS   # no leak


# --- run-scoped session sharing (§4/§5): one live session across tools in a run ---


class _FakeInner:
    """A stand-in for a real _BrowserSession: records sends and a single close, so
    the run-scoped wrapper's refcount/teardown is provable with no Docker."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = 0

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        return {"ok": True}

    async def close(self) -> None:
        self.closed += 1


def _backend_with_fake_opens() -> tuple[LocalDockerBackend, list[_FakeInner]]:
    """A backend whose open_session yields fresh fake inners (one per real lease) —
    so we can assert reuse leases NO new inner and teardown closes exactly once."""
    backend = LocalDockerBackend()
    made: list[_FakeInner] = []

    async def _fake_open_session(spec: dict) -> _FakeInner:
        inner = _FakeInner()
        made.append(inner)
        return inner

    backend.open_session = _fake_open_session  # type: ignore[method-assign,assignment]
    return backend, made


def test_spec_target_keys_on_pinned_hosts() -> None:
    assert _spec_target({"extra_hosts": {"shop.test": "1.2.3.4"}}) == "shop.test"
    assert _spec_target({}) == ""           # no pin -> reused for any same-image open


async def test_open_run_session_reuses_same_wrapper_within_a_run() -> None:
    backend, made = _backend_with_fake_opens()
    spec = {"image": "img", "extra_hosts": {"shop.test": "1.2.3.4"}}
    s1 = await backend.open_run_session(spec, run_key="run-1")
    s2 = await backend.open_run_session(spec, run_key="run-1")   # same target+run -> REUSE
    assert s1 is s2
    assert len(made) == 1 and s1.refcount == 2                   # one real lease, two holders


async def test_pointer_with_no_target_attaches_to_the_open_page() -> None:
    backend, made = _backend_with_fake_opens()
    # The Action Surface opened a pinned-target session...
    surf = await backend.open_run_session(
        {"image": "img", "extra_hosts": {"shop.test": "1.2.3.4"}}, run_key="run-1")
    # ...the pointer opens with NO target pin and gets the SAME live wrapper.
    ptr = await backend.open_run_session({"image": "img"}, run_key="run-1")
    assert ptr is surf and len(made) == 1 and surf.refcount == 2


async def test_last_release_tears_down_only_once() -> None:
    backend, made = _backend_with_fake_opens()
    spec = {"image": "img", "extra_hosts": {"shop.test": "1.2.3.4"}}
    s = await backend.open_run_session(spec, run_key="run-1")
    await backend.open_run_session(spec, run_key="run-1")        # refcount 2
    await s.close()                                              # release one -> still live
    assert made[0].closed == 0 and "run-1" in backend._sessions
    await s.close()                                              # last release -> teardown
    assert made[0].closed == 1 and "run-1" not in backend._sessions


async def test_reopen_to_different_target_leases_a_fresh_container() -> None:
    backend, made = _backend_with_fake_opens()
    s1 = await backend.open_run_session(
        {"image": "img", "extra_hosts": {"a.test": "1.1.1.1"}}, run_key="run-1")
    s2 = await backend.open_run_session(
        {"image": "img", "extra_hosts": {"b.test": "2.2.2.2"}}, run_key="run-1")
    assert s1 is not s2                                          # a new page -> a new lease
    assert made[0].closed == 1                                   # the old one torn down
    assert backend._sessions["run-1"] is s2


async def test_aclose_run_force_closes_a_lingering_session() -> None:
    backend, made = _backend_with_fake_opens()
    await backend.open_run_session(
        {"image": "img", "extra_hosts": {"shop.test": "1.2.3.4"}}, run_key="run-1")
    await backend.open_run_session(
        {"image": "img", "extra_hosts": {"shop.test": "1.2.3.4"}}, run_key="run-1")  # refcount 2
    await backend.aclose_run("run-1")                           # run end: force teardown
    assert made[0].closed == 1 and "run-1" not in backend._sessions
    await backend.aclose_run("run-1")                           # idempotent no-op
    assert made[0].closed == 1
