"""local-docker — the first SandboxBackend adapter (build step 5).

Provisions a tier's environment as a local Docker container (e.g. a
headless-browser image for tier 2) and execs tool calls inside it. Importable
now so the interface is exercised and the positioning is provable with one
adapter; the live container lifecycle is wired in build step 5.
"""

from __future__ import annotations

from typing import Any

from zu_core.ports import ToolCall


class LocalDockerBackend:
    name = "local-docker"

    async def launch(self, spec: dict) -> Any:
        raise NotImplementedError(
            "local-docker.launch is build step 5: start a container from "
            "spec['image'] with the given limits and sidecars; return a handle."
        )

    async def exec(self, sandbox: Any, call: ToolCall) -> dict:
        raise NotImplementedError(
            "local-docker.exec is build step 5: run the tool call inside the "
            "container and return its observation."
        )

    async def destroy(self, sandbox: Any) -> None:
        raise NotImplementedError(
            "local-docker.destroy is build step 5: stop and remove the container."
        )
