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
import logging
import time
from dataclasses import dataclass
from typing import Any

from zu_core.ports import ToolCall

logger = logging.getLogger(__name__)


class DockerUnavailableError(RuntimeError):
    """Raised when a render is attempted but the Docker SDK/daemon is absent."""


@dataclass
class _Sandbox:
    """A handle to a launched container. Returned by ``launch`` and passed back
    to ``exec``/``destroy`` — opaque to the loop, which only moves it around."""

    container: Any
    image: str


# The render command run inside the browser container. The image is expected to
# ship a small entrypoint that takes a URL on argv and prints a JSON line
# ``{"status", "html", "url"}`` to stdout — the same observation shape
# http_fetch produces, so the loop and detectors stay tool-agnostic.
_RENDER_ENTRYPOINT = "zu-render"


class LocalDockerBackend:
    name = "local-docker"

    def __init__(self, client: Any = None, *, startup_timeout_s: int = 30) -> None:
        # client is a testability/config seam (an already-built docker client);
        # None -> connect to the local daemon from the environment on first use.
        self._client = client
        self.startup_timeout_s = startup_timeout_s

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
            self._client = docker.from_env()
        except Exception as exc:  # pragma: no cover - daemon-dependent
            raise DockerUnavailableError(
                f"could not connect to the Docker daemon: {exc}"
            ) from exc
        return self._client

    async def launch(self, spec: dict) -> _Sandbox:
        """Start a detached container from ``spec['image']`` with the network
        locked down by default (the tier's egress policy lives here, not in the
        host-level SSRF guard) and return an opaque handle."""
        image = spec["image"]
        client = self._docker()
        # The Docker SDK is synchronous and a container launch is seconds-long;
        # run it in a worker thread so it never blocks the event loop (and other
        # concurrent runs) for the duration. Same rationale for exec/destroy.
        container = await asyncio.to_thread(
            client.containers.run,
            image,
            detach=True,
            # No network by default: the sandbox is where a tier's egress policy
            # is enforced. A tier that needs the public web opts in via spec.
            network_disabled=not spec.get("network", False),
            mem_limit=spec.get("mem_limit", "1g"),
            # Privilege hardening, secure by default: this container runs an
            # untrusted, model-chosen URL. Drop all Linux capabilities and forbid
            # privilege escalation; a browser image that genuinely needs a cap
            # (e.g. SYS_ADMIN for Chromium's sandbox) opts back in via cap_add,
            # and pids_limit caps a fork bomb. read_only is opt-in (a browser
            # needs a writable /tmp), exposed so a locked-down image can set it.
            cap_drop=spec.get("cap_drop", ["ALL"]),
            cap_add=spec.get("cap_add", []),
            security_opt=spec.get("security_opt", ["no-new-privileges"]),
            pids_limit=spec.get("pids_limit", 256),
            read_only=spec.get("read_only", False),
            # Don't keep the container around after it stops; we also destroy
            # explicitly, but this is the backstop against a leak on crash.
            auto_remove=False,
        )
        await self._await_running(container)
        return _Sandbox(container=container, image=image)

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
        url = call.args["url"]
        exit_code, output = await asyncio.to_thread(
            sandbox.container.exec_run, [_RENDER_ENTRYPOINT, url]
        )
        text = output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output)
        if exit_code != 0:
            return {"status": 500, "html": "", "error": f"render failed (exit {exit_code}): {text[:500]}"}
        import json

        try:
            return json.loads(text)
        except ValueError:
            # The entrypoint should print JSON; if it printed raw HTML, treat
            # the whole stdout as the page so a render is never silently lost.
            return {"status": 200, "html": text}

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
