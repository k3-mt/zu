"""local-docker — the first SandboxBackend adapter.

Provisions a tier's environment as a local Docker container (e.g. a
headless-browser image for tier 2) and execs tool calls inside it. This is the
host-local default; heavier isolation (microVMs, Modal, E2B, Browserbase)
arrives as additional ``SandboxBackend`` adapters, never a change to the loop.

The ``docker`` SDK is an optional dependency (``zu-backends[docker]``) and is
imported lazily, so importing this module — for discovery, or to register it —
never requires Docker to be installed or running. The daemon is only touched
when a sandbox is actually launched.

A note on testing: the live browser is the unpredictable part, so the
escalation ladder is proven offline against a *scripted* SandboxBackend that
replays a saved rendered page (see the loop tests). This adapter is what runs
in production; it is exercised against a real daemon, opt-in, the same way the
real model providers are (build step 7).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from zu_core.ports import ToolCall

logger = logging.getLogger(__name__)


class DockerUnavailableError(RuntimeError):
    """Raised when a render is attempted but the Docker SDK/daemon is absent."""


@dataclass
class _Sandbox:
    """A handle to a launched container. Returned by ``launch`` and passed back
    to ``exec``/``destroy`` — opaque to the loop, which only moves it around.

    ``cleanup_paths`` are host temp files (e.g. a per-run TLS-intercept CA
    bind-mounted into the container) to remove at ``destroy``, so an enabled
    egress interception leaves nothing on the host after the run."""

    container: Any
    image: str
    cleanup_paths: list[str] = field(default_factory=list)


# The render command run inside the browser container. The image is expected to
# ship a small entrypoint that takes a URL on argv and prints a JSON line
# ``{"status", "html", "url"}`` to stdout — the same observation shape
# http_fetch produces, so the loop and detectors stay tool-agnostic.
_RENDER_ENTRYPOINT = "zu-render"
# The persistent session server: a long-lived process holding ONE browser, fed
# newline-delimited JSON commands on stdin, one JSON response per line on stdout.
_SESSION_ENTRYPOINT = "zu-browser"


@dataclass
class _BrowserSession:
    """A live browser session: a kept-alive container + an open exec stream to the
    ``zu-browser`` server inside it. Commands (open/act/read/close) are written as
    JSON lines and a JSON response is read back; the browser state persists between
    them. Docker multiplexes the exec stream in 8-byte-framed chunks, so reads
    demux the stdout stream and buffer to whole lines."""

    backend: LocalDockerBackend
    sandbox: _Sandbox
    sock: Any
    read_timeout_s: float = 120.0
    _buf: bytes = field(default=b"", init=False)

    def _raw(self) -> Any:
        return getattr(self.sock, "_sock", self.sock)

    @staticmethod
    def _recvn(s: Any, n: int) -> bytes:
        out = b""
        while len(out) < n:
            chunk = s.recv(n - len(out))
            if not chunk:
                break
            out += chunk
        return out

    def _read_line_sync(self) -> str:
        s = self._raw()
        s.settimeout(self.read_timeout_s)
        while b"\n" not in self._buf:
            header = self._recvn(s, 8)  # [stream(1), 0,0,0, size(4 big-endian)]
            if len(header) < 8:
                break
            size = int.from_bytes(header[4:8], "big")
            payload = self._recvn(s, size)
            if header[0] == 1:  # stdout (stderr=2 is Chromium noise; ignore)
                self._buf += payload
            if not payload:
                break
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("utf-8", "replace")

    def _send_sync(self, cmd: dict) -> dict:
        self._raw().sendall((json.dumps(cmd) + "\n").encode())
        line = self._read_line_sync()
        if not line.strip():
            return {"error": "session closed or no response"}
        return json.loads(line)

    async def send(self, cmd: dict) -> dict:
        """Write one command and read its JSON response (off the event loop)."""
        return await asyncio.to_thread(self._send_sync, cmd)

    async def close(self) -> None:
        """Tell the server to close its browser, then remove the container."""
        try:
            await asyncio.wait_for(self.send({"op": "close"}), timeout=15)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass
        try:
            self._raw().close()
        except Exception:  # noqa: BLE001
            pass
        await self.backend.destroy(self.sandbox)


@dataclass
class _RunScopedSession:
    """A refcounted wrapper that lets one browser session be SHARED across tools in
    a single run (e.g. ``action_surface(op=open)`` opens it; ``pointer`` reuses the
    SAME live page). It is additive over :class:`_BrowserSession`: ``send`` delegates
    verbatim, and ``close`` is a REF RELEASE — it decrements and only tears the real
    session down (and de-registers from the backend) when the last holder releases.

    The whole point is that a pointer acting on a page opened by the Action Surface
    must hit the SAME container — not a fresh browser with no page. Keying lives on
    the backend (``open_run_session``), the smallest blast radius: zu-core owns no
    Docker/browser lifecycle and a live socket is never folded onto RunContext."""

    backend: LocalDockerBackend
    inner: _BrowserSession
    run_key: str
    target: str
    refcount: int = 1

    async def send(self, cmd: dict) -> dict:
        return await self.inner.send(cmd)

    async def close(self) -> None:
        """Release ONE ref; tear the real session down only on the last release."""
        self.refcount -= 1
        if self.refcount > 0:
            return
        self.backend._sessions.pop(self.run_key, None)
        await self.inner.close()


def _spec_target(spec: dict) -> str:
    """A stable key for the page a session is pointed at, so a run-scoped reuse only
    matches the SAME target. The Action Surface / Browser pin the target host via
    ``extra_hosts`` (host→ip); we key on that (sorted) so a re-open to a different
    host leases a fresh container rather than acting on the wrong page. Empty when
    no host pin is present (the session is reused for any same-image open)."""
    hosts = spec.get("extra_hosts") or {}
    if not isinstance(hosts, dict):
        return ""
    return ",".join(sorted(hosts))


def _render_argv(args: dict) -> list[str]:
    """Build the ``zu-render`` argv from a render ToolCall's args. Purely generic —
    a URL plus optional viewport and wait/reveal flags; no site-specific logic. The
    model supplies whatever url/wait/actions its reasoning calls for; this only
    serializes them onto the entrypoint's CLI."""
    argv = [_RENDER_ENTRYPOINT, str(args["url"])]
    width, height = args.get("width"), args.get("height")
    if width and height:
        argv += ["--width", str(int(width)), "--height", str(int(height))]
    if args.get("wait_until"):
        argv += ["--wait-until", str(args["wait_until"])]
    if args.get("wait_for"):
        argv += ["--wait-for", str(args["wait_for"])]
    if args.get("wait_ms"):
        argv += ["--wait-ms", str(int(args["wait_ms"]))]
    if args.get("actions"):
        argv += ["--actions", json.dumps(args["actions"])]
    if args.get("capture_network"):
        argv.append("--capture-network")
    return argv


class ImagePolicyError(RuntimeError):
    """Raised when ``launch`` refuses an image that fails the pull policy (F34):
    a registry not on the allowlist, or a floating tag when a digest is required.
    Distinct from :class:`DockerUnavailableError` so a policy refusal is never
    mistaken for a missing daemon."""


class NetworkPolicyError(RuntimeError):
    """Raised when ``launch`` refuses unrestricted egress (``network: True``)
    without the explicit opt-in flag (F38): unrestricted egress must be a
    deliberate, logged choice, never a silent default."""


def _image_registry(image: str) -> str:
    """The registry host of a Docker image reference, or ``""`` for Docker Hub's
    implicit ``docker.io`` (a bare ``name`` or ``ns/name``). Purely lexical — the
    first path segment is a registry only if it looks like a host (contains a
    ``.`` or ``:``, or is ``localhost``), matching Docker's own reference grammar.
    A ``@sha256:`` digest or ``:tag`` on the final segment does not affect this."""
    ref = image.split("@", 1)[0]  # drop any digest before splitting on '/'
    head = ref.split("/", 1)[0]
    if head == "localhost" or "." in head or ":" in head:
        return head
    return ""


def _image_is_digest_pinned(image: str) -> bool:
    """True when the image reference names an immutable ``@sha256:`` digest — so
    Docker's auto-pull resolves to exactly one content-addressed image, not a
    mutable tag a registry could repoint."""
    return "@sha256:" in image


def _split_csv_env(raw: str | None) -> list[str]:
    """Parse a comma/space-separated env value into a clean list (empties dropped).
    Used for the registry allowlist so it is env/config-derived, never hardcoded."""
    if not raw:
        return []
    return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]


class LocalDockerBackend:
    name = "local-docker"

    # The default registry allowlist (F34): a bare/`ns/name` image (implicit
    # ``docker.io``, registry ``""``) is always permitted so existing test/render
    # images keep working, and ``localhost`` (a locally-built image) is trusted.
    # This is a floor, not a hardcode: the effective allowlist is the union of this,
    # the ``ZU_IMAGE_REGISTRY_ALLOWLIST`` env, and the per-launch ``image_allowlist``
    # spec key — the project namespace is supplied by config, not baked in here.
    _DEFAULT_REGISTRY_ALLOWLIST = ("", "localhost")

    def __init__(
        self, client: Any = None, *, startup_timeout_s: int = 30, exec_timeout_s: int = 45
    ) -> None:
        # client is a testability/config seam (an already-built docker client);
        # None -> connect to the local daemon from the environment on first use.
        self._client = client
        self.startup_timeout_s = startup_timeout_s
        # Overall deadline for one in-container render; a hair above the
        # entrypoint's own 30s page timeout so the inner timeout normally wins
        # and we only trip on a truly wedged exec.
        self.exec_timeout_s = exec_timeout_s
        # Run-scoped session registry (keyed by run id): the lease point that lets
        # the Action Surface and the pointer SHARE one live browser within a run.
        # Empty until ``open_run_session`` is used; ``open_session`` is untouched, so
        # every existing open-close-per-call path is unaffected.
        self._sessions: dict[str, _RunScopedSession] = {}

    def _docker(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - exercised by deselected env
            raise DockerUnavailableError(
                "render_dom needs the Docker SDK: install zu-backends[docker] "
                "and ensure a Docker daemon is running, or inject a SandboxBackend."
            ) from exc
        try:
            # version="auto" negotiates the daemon's API version. Without it the SDK
            # requests its own (newer) default and 404s on older daemons — e.g. the CI
            # runner's Docker — at containers/create.
            self._client = docker.from_env(version="auto")
        except Exception as exc:  # pragma: no cover - daemon-dependent
            raise DockerUnavailableError(
                f"could not connect to the Docker daemon: {exc}"
            ) from exc
        return self._client

    def _check_image_policy(self, image: str, spec: dict) -> None:
        """Guard the image before Docker's implicit auto-pull (F34): an arbitrary
        ``spec['image']`` would otherwise let a run name ANY registry/image and
        have Docker pull it. Two configurable, generic controls, both default-off
        for the existing test/render images:

          * a REGISTRY ALLOWLIST — the union of the built-in floor
            (``docker.io``/``localhost``), the ``ZU_IMAGE_REGISTRY_ALLOWLIST`` env,
            and the per-launch ``spec['image_allowlist']``. An image whose registry
            is not on it is refused. The project's own namespace is supplied by
            config, not hardcoded here.
          * a DIGEST-PIN requirement — when ``spec['require_digest']`` is set (or
            ``ZU_IMAGE_REQUIRE_DIGEST=1``), a floating ``:tag`` is refused; only an
            ``@sha256:`` reference (one immutable, content-addressed image) is run.

        The decision is logged either way, so the policy that let (or blocked) a
        pull is auditable."""
        allowlist = {
            *self._DEFAULT_REGISTRY_ALLOWLIST,
            *_split_csv_env(os.environ.get("ZU_IMAGE_REGISTRY_ALLOWLIST")),
            *(spec.get("image_allowlist") or ()),
        }
        registry = _image_registry(image)
        if registry not in allowlist:
            logger.warning(
                "refusing image %r: registry %r not on allowlist %s",
                image, registry, sorted(allowlist),
            )
            raise ImagePolicyError(
                f"image {image!r} names registry {registry or 'docker.io'!r}, which is "
                f"not on the allowlist {sorted(allowlist)}; add it via "
                f"ZU_IMAGE_REGISTRY_ALLOWLIST or spec['image_allowlist']."
            )
        require_digest = bool(spec.get("require_digest")) or bool(
            _split_csv_env(os.environ.get("ZU_IMAGE_REQUIRE_DIGEST"))
        )
        if require_digest and not _image_is_digest_pinned(image):
            logger.warning("refusing image %r: digest pin required but reference is a tag", image)
            raise ImagePolicyError(
                f"image {image!r} is not digest-pinned; policy requires an @sha256: "
                f"reference (set spec['require_digest']=False to opt out)."
            )
        logger.debug(
            "image policy passed for %r (registry=%r, digest_pinned=%s)",
            image, registry, _image_is_digest_pinned(image),
        )

    async def launch(self, spec: dict) -> _Sandbox:
        """Start a detached container from ``spec['image']`` and return an opaque
        handle. Network has three modes:

          * absent/false  -> ``network_disabled`` (the render default; no egress);
          * truthy        -> network on, EXPLICITLY UNRESTRICTED (the public-web
            tier). This is the one mode with no egress enforcement, so it must be
            opted into deliberately: pass ``spec['allow_unrestricted_egress']=True``
            (or set ``ZU_ALLOW_UNRESTRICTED_EGRESS=1``) or the launch is refused
            (F38). Callers wanting ENFORCED egress should use ``"isolated"`` below;
            the choice to go unrestricted is logged.
          * ``"isolated"`` -> attach to the pre-created INTERNAL docker network
            ``spec['network_name']`` (no external route), so the egress proxy is
            the **only** path off-box. The internal network is the default-DROP
            enforcement for the contained-egress container form; ``spec['proxy']``
            then injects HTTP(S)_PROXY so a cooperative client routes through it —
            the env is convenience, the network is the guarantee.

        The image is guarded before launch by :meth:`_check_image_policy` (F34):
        a registry allowlist and an optional digest-pin requirement, so an
        arbitrary image cannot be auto-pulled from an arbitrary registry.

        ``spec['proxy']`` (``{host, port}``) injects proxy env; ``spec['extra_hosts']``
        DNS-pins validated host->IP; the cap-drop/no-new-privileges/pids hardening
        is unchanged."""
        image = spec["image"]
        self._check_image_policy(image, spec)
        client = self._docker()
        # Privilege hardening with a FLOOR the spec can tighten but not loosen:
        # this container runs untrusted, model-chosen work. ``cap_drop`` always
        # includes ALL and ``security_opt`` always includes no-new-privileges, even
        # if the spec omits them or passes an empty list — a caller cannot silently
        # remove the baseline. A browser image that needs a cap still opts back in
        # via ``cap_add`` (the standard drop-ALL-then-add pattern); ``pids_limit``
        # is forced to a positive value so a fork bomb is always capped.
        cap_drop = list(spec.get("cap_drop") or ["ALL"])
        if "ALL" not in cap_drop:
            cap_drop = ["ALL", *cap_drop]
        security_opt = list(spec.get("security_opt") or ["no-new-privileges"])
        if "no-new-privileges" not in security_opt:
            security_opt = [*security_opt, "no-new-privileges"]
        pids_limit = spec.get("pids_limit", 256)
        if not isinstance(pids_limit, int) or pids_limit <= 0:
            pids_limit = 256
        run_kwargs: dict = dict(  # noqa: C408 - keyword form keeps the inline hardening notes readable
            detach=True,
            # DNS pin: map the validated target host -> validated IP in the
            # container's /etc/hosts, so the browser cannot be rebound to an
            # internal address at connect time.
            extra_hosts=spec.get("extra_hosts") or {},
            mem_limit=spec.get("mem_limit", "1g"),
            cap_drop=cap_drop,
            cap_add=spec.get("cap_add", []),
            security_opt=security_opt,
            pids_limit=pids_limit,
            # read_only is opt-in (a browser needs a writable /tmp), exposed so a
            # locked-down image can set it — a tightening, so left to the spec.
            read_only=spec.get("read_only", False),
            # Don't keep the container around after it stops; we also destroy
            # explicitly, but this is the backstop against a leak on crash.
            auto_remove=False,
        )
        network = spec.get("network", False)
        if network == "isolated":
            # The only route off-box is the proxy on this internal network.
            run_kwargs["network"] = spec["network_name"]
            run_kwargs["network_disabled"] = False
        elif network:
            # Unrestricted egress (F38): no proxy, no default-DROP — the container
            # can reach anything. This must be a deliberate, logged choice, never a
            # silent default: refuse unless the caller explicitly opted in (or steer
            # them to network="isolated" for enforced egress).
            opt_in = bool(spec.get("allow_unrestricted_egress")) or bool(
                _split_csv_env(os.environ.get("ZU_ALLOW_UNRESTRICTED_EGRESS"))
            )
            if not opt_in:
                raise NetworkPolicyError(
                    "network=True grants UNRESTRICTED egress (no proxy / no default-DROP). "
                    "Opt in explicitly via spec['allow_unrestricted_egress']=True or "
                    "ZU_ALLOW_UNRESTRICTED_EGRESS=1, or use network='isolated' for "
                    "enforced egress through the proxy."
                )
            logger.warning(
                "launching %r with UNRESTRICTED egress (network=True, no egress proxy) "
                "by explicit opt-in", image,
            )
            run_kwargs["network_disabled"] = False
        else:
            run_kwargs["network_disabled"] = True
        # DNS gating (ZU-NET-1): when an EgressEnforcement supplies ``dns`` (e.g.
        # a non-resolving nameserver so the embedded resolver cannot be used as a
        # covert egress channel), set it on the container — the proxy is reached by
        # its pinned ``extra_hosts`` IP, so name resolution is unnecessary. Omitted
        # ⇒ Docker's default, so existing runs are unchanged.
        if spec.get("dns"):
            run_kwargs["dns"] = list(spec["dns"])
        # Optional seccomp profile: an AUDIT profile LOGs sensitive syscalls; a
        # BLOCKING profile ERRNOs the escape primitives. The profile is supplied by
        # the caller (path/JSON/"unconfined"); this backend names no specific one.
        # Appended to security_opt. NOTE: the docker SDK passes
        # the value to the daemon verbatim — unlike the CLI it does NOT read a file
        # — so a path must be inlined to its JSON content here, or the daemon fails
        # with "Decoding seccomp profile failed". A profile given as JSON (starts
        # with '{') or the special value "unconfined" is passed through as-is.
        seccomp = spec.get("seccomp")
        if seccomp:
            profile = str(seccomp)
            if profile != "unconfined" and not profile.lstrip().startswith("{"):
                with open(profile, encoding="utf-8") as fh:
                    profile = fh.read()
            run_kwargs["security_opt"] = [*run_kwargs.get("security_opt", []), f"seccomp={profile}"]
        # Bind mounts requested by the caller (e.g. a bundle's tools/ mounted
        # read-only). Applied unconditionally; the CA branches below merge into it.
        if spec.get("volumes"):
            run_kwargs["volumes"] = dict(spec["volumes"])
        environment = dict(spec.get("environment") or {})
        proxy = spec.get("proxy")
        if proxy:
            url = f"http://{proxy['host']}:{proxy['port']}"
            environment.update({
                "HTTP_PROXY": url, "HTTPS_PROXY": url,
                "http_proxy": url, "https_proxy": url,
                "NO_PROXY": spec.get("no_proxy", "localhost,127.0.0.1"),
            })
        # Per-run TLS-intercept CA: write it to a host temp file, bind-mount it
        # read-only into the container, and point the standard TLS-trust env vars
        # at it so the in-container client trusts the egress proxy's minted leaves.
        # The temp file is tracked for removal at destroy — the CA dies with the run.
        cleanup_paths: list[str] = []
        ca_cert = spec.get("ca_cert")
        if ca_cert:
            fd, ca_path = tempfile.mkstemp(suffix="-zu-tls-intercept-ca.pem")
            os.write(fd, ca_cert if isinstance(ca_cert, bytes) else str(ca_cert).encode())
            os.close(fd)
            cleanup_paths.append(ca_path)
            # The default in-container mount path is a stable, consumer-facing
            # value (kept for compatibility with existing callers that rely on it);
            # it is opaque and carries no policy meaning. Override via ``ca_mount``.
            mount = spec.get("ca_mount", "/zu-redteam-ca.pem")
            run_kwargs["volumes"] = {**(spec.get("volumes") or {}),
                                     ca_path: {"bind": mount, "mode": "ro"}}
            environment.update({"SSL_CERT_FILE": mount, "REQUESTS_CA_BUNDLE": mount})
        # Shared-volume CA (sidecar topology): the proxy sidecar writes its per-run
        # CA into a docker volume; the target mounts the SAME volume read-only and
        # trusts it. No host file — the CA lives only in the volume and the run.
        ca_volume = spec.get("ca_volume")
        if ca_volume:
            mdir = spec.get("ca_volume_mount", "/ca")
            ca_file = f"{mdir}/ca.pem"
            run_kwargs["volumes"] = {**(run_kwargs.get("volumes") or spec.get("volumes") or {}),
                                     ca_volume: {"bind": mdir, "mode": "ro"}}
            environment.update({"SSL_CERT_FILE": ca_file, "REQUESTS_CA_BUNDLE": ca_file})
        if environment:
            run_kwargs["environment"] = environment
        # An optional command override keeps a non-render target alive (e.g.
        # ``sleep infinity``) so the sidecar gate can exec the runner into it,
        # rather than running the image's default server CMD.
        if spec.get("command") is not None:
            run_kwargs["command"] = spec["command"]
        # The Docker SDK is synchronous and a container launch is seconds-long;
        # run it in a worker thread so it never blocks the event loop (and other
        # concurrent runs) for the duration. Same rationale for exec/destroy.
        try:
            container = await asyncio.to_thread(client.containers.run, image, **run_kwargs)
        except Exception:
            for p in cleanup_paths:  # don't leak the CA temp file on a failed launch
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise
        # If the container never reaches "running" (exited/dead/timeout), tear it
        # down here: with auto_remove=False a startup failure would otherwise
        # leave a stopped container behind on every failed launch, since the
        # caller gets an exception instead of a handle to destroy.
        try:
            await self._await_running(container)
        except Exception:
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception as exc:  # noqa: BLE001 - cleanup must not mask the launch error
                logger.warning(
                    "failed to remove container %s after a failed launch: %s",
                    getattr(container, "id", "?"), exc,
                )
            for p in cleanup_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            raise
        return _Sandbox(container=container, image=image, cleanup_paths=cleanup_paths)

    async def _await_running(self, container: Any) -> None:
        """Poll until the container reports ``running`` (or exits), bounded by
        ``startup_timeout_s`` — so a render never execs into a not-yet-ready or
        already-dead container. Defensive about SDK shape so a minimal injected
        client (no reload/status) is simply treated as ready."""
        reload = getattr(container, "reload", None)
        if reload is None:
            return  # injected stub without a lifecycle; nothing to wait on
        deadline = time.monotonic() + self.startup_timeout_s
        while True:
            await asyncio.to_thread(reload)
            status = getattr(container, "status", "running")
            if status == "running":
                return
            if status in ("exited", "dead"):
                raise DockerUnavailableError(
                    f"render container entered status {status!r} before becoming ready"
                )
            if time.monotonic() >= deadline:
                raise DockerUnavailableError(
                    f"render container not running after {self.startup_timeout_s}s "
                    f"(last status {status!r})"
                )
            await asyncio.sleep(0.05)

    async def exec(self, sandbox: _Sandbox, call: ToolCall) -> dict:
        """Run the tool call inside the container and return its observation."""
        argv = _render_argv(call.args)
        # demux=True keeps stdout and stderr SEPARATE. Chromium under
        # --no-sandbox is extremely noisy on stderr (GPU/font/dbus warnings); if
        # that noise were merged into stdout it would corrupt the single JSON
        # line the entrypoint prints and turn successful renders into "non-JSON
        # output" errors. We parse stdout only; stderr is kept for diagnostics.
        # Bounded by an overall deadline so a wedged in-container render can't
        # hang the awaiting coroutine forever (the entrypoint also self-times-out).
        try:
            exit_code, streams = await asyncio.wait_for(
                asyncio.to_thread(sandbox.container.exec_run, argv, demux=True),
                timeout=self.exec_timeout_s,
            )
        except TimeoutError:
            return {"status": 504, "html": "",
                    "error": f"render timed out after {self.exec_timeout_s}s"}
        stdout, stderr = streams if isinstance(streams, tuple) else (streams, None)
        text = stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout or "")
        if exit_code != 0:
            err = (stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else "") or text
            return {"status": 500, "html": "", "error": f"render failed (exit {exit_code}): {err[:500]}"}
        try:
            return json.loads(text)
        except ValueError:
            # The entrypoint contract is a JSON line; non-JSON stdout is a broken
            # render, not a page. Surfacing it as {"status": 200, "html": text}
            # would launder garbage into a successful observation that the
            # detectors trust — so return an ERROR observation instead, which the
            # `error` detector fires on. Raw stdout is preserved (capped) for
            # debugging, never presented as page content.
            return {
                "status": 500,
                "error": "render produced non-JSON output",
                "raw": text[:2000],
            }

    async def exec_entrypoint(
        self, sandbox: _Sandbox, argv: list[str], *,
        environment: dict | None = None, timeout_s: float | None = None,
    ) -> tuple[int, str, str]:
        """Run an arbitrary argv in the container and return ``(exit_code, stdout,
        stderr)``. Generalises :meth:`exec` (which is render-specific) so a caller
        can exec its own runner inside the box — passing a spec via an environment
        variable of its choosing — and read its JSONL event log off stdout. Bounded
        by ``timeout_s`` (default the backend's ``exec_timeout_s``) so a wedged run
        can't hang forever."""
        timeout = self.exec_timeout_s if timeout_s is None else timeout_s
        try:
            exit_code, streams = await asyncio.wait_for(
                asyncio.to_thread(
                    sandbox.container.exec_run, argv, demux=True,
                    environment=environment or {},
                ),
                timeout=timeout,
            )
        except TimeoutError:
            return 504, "", f"exec timed out after {timeout}s"
        stdout, stderr = streams if isinstance(streams, tuple) else (streams, None)
        out = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else str(stdout or "")
        err = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr or "")
        return exit_code, out, err

    # Docker's diff Kind codes: 0 modified, 1 added, 2 deleted.
    _DIFF_KINDS = {0: "changed", 1: "added", 2: "deleted"}

    async def top(self, sandbox: _Sandbox) -> list[dict]:
        """The target's process table (``docker top``) as ``[{pid, cmd}]`` — the
        out-of-band view of what is running, read before teardown. Portable (any
        Docker host), so it catches a *persistent* process a tool spawned without
        declaring ``subprocess`` (a transient exec that exits between reads needs
        the seccomp-audit source instead)."""
        info = await asyncio.to_thread(sandbox.container.top)
        titles = info.get("Titles") or []
        rows = info.get("Processes") or []
        try:
            ci = titles.index("CMD")
        except ValueError:
            ci = len(titles) - 1 if titles else -1
        try:
            pi = titles.index("PID")
        except ValueError:
            pi = 1
        out: list[dict] = []
        for row in rows:
            cmd = row[ci] if 0 <= ci < len(row) else " ".join(row)
            pid = row[pi] if 0 <= pi < len(row) else ""
            out.append({"pid": pid, "cmd": cmd})
        return out

    async def mounts(self, sandbox: _Sandbox) -> list[dict]:
        """The target's mounts (``container.attrs['Mounts']``) — so a writable host
        bind-mount (a filesystem-escape path the sandbox should never have) is
        visible to the mount-escape check."""
        await asyncio.to_thread(sandbox.container.reload)
        return list(sandbox.container.attrs.get("Mounts") or [])

    async def fs_diff(self, sandbox: _Sandbox) -> list[dict]:
        """The container's filesystem changes since launch (``docker diff``), as
        ``[{path, kind}]`` — the out-of-band record of what the run *wrote*, read
        AFTER the run and BEFORE teardown. This is the host-effect audit source a
        containment consumer reads: a plugin that modified the filesystem is
        visible here whether or not it admitted to it. Defensive about the SDK
        shape so an injected stub still works."""
        raw = await asyncio.to_thread(sandbox.container.diff)
        out: list[dict] = []
        for d in raw or []:
            out.append({"path": d.get("Path"), "kind": self._DIFF_KINDS.get(d.get("Kind"), "changed")})
        return out

    async def open_session(self, spec: dict) -> _BrowserSession:
        """Launch a hardened, kept-alive container and exec the persistent
        ``zu-browser`` session server into it, returning a handle whose stdin/stdout
        stay open across many commands. This is what holds one headless browser
        ALIVE across tool calls so a model can drive a reactive widget
        incrementally (open→act→read→close) instead of replaying into a fresh
        browser each time. Same launch hardening as a render (caps dropped, DNS pin,
        seccomp); the container's default ``sleep infinity`` keeps it up."""
        sandbox = await self.launch(spec)
        api = sandbox.container.client.api
        created = await asyncio.to_thread(
            api.exec_create, sandbox.container.id, [_SESSION_ENTRYPOINT],
            stdin=True, stdout=True, stderr=True, tty=False,
        )
        sock = await asyncio.to_thread(api.exec_start, created["Id"], socket=True, demux=False)
        return _BrowserSession(backend=self, sandbox=sandbox, sock=sock)

    async def open_run_session(self, spec: dict, *, run_key: str) -> _RunScopedSession:
        """Open (or REUSE) a browser session scoped to a run, so tools share one live
        page. The first ``action_surface(op=open)``/``browser(op=open)`` of a run
        creates the session (refcount=1) under ``run_key``; a later ``pointer`` (or a
        second tool) with the SAME ``run_key`` AND the same target gets the SAME
        wrapper with refcount bumped — the shared live page the pointer must act on.

        A re-open to a DIFFERENT target releases the prior ref and leases a fresh
        container, so a model navigating elsewhere mid-run is honoured. ``close`` on
        the returned wrapper is a ref release; the real container tears down only on
        the last release. ``open_session`` is never touched, so the one-shot
        open-close-per-call path (and every test injecting a fake) is unaffected."""
        target = str(spec.get("image", "")) + "|" + _spec_target(spec)
        existing = self._sessions.get(run_key)
        # Reuse when the target matches OR when this caller pins no specific target
        # (the pointer, which has no url of its own — it ATTACHES to whatever live
        # page this run already opened). A mismatched, explicitly-pinned target falls
        # through to a fresh lease below.
        if existing is not None and (existing.target == target or _spec_target(spec) == ""):
            existing.refcount += 1
            return existing
        if existing is not None:
            # A re-open to a different target: drop our hold on the old one (it tears
            # down when its last holder releases) and lease a fresh session below.
            self._sessions.pop(run_key, None)
            await existing.close()
        inner = await self.open_session(spec)
        wrapper = _RunScopedSession(backend=self, inner=inner, run_key=run_key, target=target)
        self._sessions[run_key] = wrapper
        return wrapper

    async def aclose_run(self, run_key: str) -> None:
        """Force-close any session still registered for ``run_key`` — the run-end
        teardown backstop so a shared container never outlives its run regardless of
        whether each tool released its ref. A no-op when nothing is registered."""
        wrapper = self._sessions.pop(run_key, None)
        if wrapper is not None:
            wrapper.refcount = 0  # this is the authoritative last release
            await wrapper.inner.close()

    async def destroy(self, sandbox: _Sandbox) -> None:
        """Stop and remove the container. Best-effort: a teardown failure must
        not raise over the render's own result, but it IS logged at WARNING — a
        silent swallow turns a leaked container into an invisible resource leak."""
        try:
            await asyncio.to_thread(sandbox.container.remove, force=True)
        except Exception as exc:  # noqa: BLE001 - teardown must not raise over the result
            logger.warning(
                "failed to remove render container %s: %s",
                getattr(sandbox.container, "id", "?"),
                exc,
            )
        for p in getattr(sandbox, "cleanup_paths", []):  # remove the per-run TLS-intercept CA
            try:
                os.unlink(p)
            except OSError:
                pass
