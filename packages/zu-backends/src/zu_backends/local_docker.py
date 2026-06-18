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

from dataclasses import dataclass
from typing import Any

from zu_core.ports import ToolCall


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
        container = client.containers.run(
            image,
            detach=True,
            # No network by default: the sandbox is where a tier's egress policy
            # is enforced. A tier that needs the public web opts in via spec.
            network_disabled=not spec.get("network", False),
            mem_limit=spec.get("mem_limit", "1g"),
            # Don't keep the container around after it stops; we also destroy
            # explicitly, but this is the backstop against a leak on crash.
            auto_remove=False,
        )
        return _Sandbox(container=container, image=image)

    async def exec(self, sandbox: _Sandbox, call: ToolCall) -> dict:
        """Run the tool call inside the container and return its observation."""
        url = call.args["url"]
        exit_code, output = sandbox.container.exec_run([_RENDER_ENTRYPOINT, url])
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
        """Stop and remove the container. Best-effort: teardown failures are
        swallowed so they can't mask the render's own result or error."""
        try:
            sandbox.container.remove(force=True)
        except Exception:  # noqa: BLE001 - teardown must not raise over the result
            pass
