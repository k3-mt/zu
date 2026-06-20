"""Whole-agent-in-container containment — the launcher for ``containment: required``.

How runs actually get contained: the agent cannot police a hostile *tool* in
process — a tool is just Python running in your interpreter, so by the time the
loop sees a call the tool's code has already run. Real tool containment is an OS
boundary. This launcher runs the **entire agent inside a hardened container**
whose only route off-box is an egress proxy on an internal (default-DROP)
network, with all caps dropped, no-new-privileges, and a blocking seccomp
profile. Inside that box ``ZU_SANDBOXED=1`` is set, so the fail-closed floor
(:func:`zu_core.security.enforce_containment`) is satisfied and tools may run —
the container, not the loop, is what contains them.

Two halves:

* :func:`run_contained_from_env` — the in-container entrypoint (console script
  ``zu-run-contained``). Reads the task + config from the environment, runs the
  agent, and writes ``{"result": ..., "events": [...]}`` as one JSON object on
  stdout. It runs *as contained* only because the launcher set ``ZU_SANDBOXED``.
* :class:`SandboxLauncher` — the host side. Launches the proxy, launches the
  hardened container on the internal network, execs the entrypoint with the
  task/config in its env, parses the Result back, and tears everything down.

The backend and proxy are injected (``LocalDockerBackend`` + ``LocalEgressProxy``
in production, fakes in tests), so the orchestration is exercised without a
daemon. The Docker daemon itself is the only un-fakeable part — the same P0/P1
boundary the red-team container form documents.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any

from zu_core.contracts import Result
from zu_core.loop import run_task
from zu_core.security import SANDBOX_ENV


def _seccomp_block_profile() -> str:
    """The host path of the shipped blocking seccomp profile (Docker reads the
    profile from the client host). Resolved lazily so importing this module never
    requires zu-backends to be installed."""
    from pathlib import Path

    import zu_backends

    return str(Path(zu_backends.__file__).parent / "seccomp" / "redteam-block.json")


def _last_json_object(out: str) -> dict:
    """Parse the last non-empty line of stdout as the result JSON. The entrypoint
    writes exactly one JSON object, but taking the last line tolerates any
    incidental log line the image might print before it."""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line:
            return json.loads(line)
    raise ValueError("no output to parse")


async def _run_in_process(task: dict, config: dict) -> tuple[Result, list]:
    """Run one task in this process and return (Result, events). Used by the
    in-container entrypoint, where ``ZU_SANDBOXED`` is already set by the launcher
    so the containment floor passes."""
    from .config import assemble, coerce_config, coerce_task

    cfg = coerce_config(config)
    spec = coerce_task(task, cfg.budget, allow_paths=False)
    provider, registry, bus, providers = assemble(cfg)
    try:
        result = await run_task(
            spec, provider, registry, bus,
            providers=providers, containment=cfg.containment,
        )
        return result, await bus.query()
    finally:
        await bus.aclose()


def run_contained_from_env(argv: list[str] | None = None) -> int:
    """Console-script entrypoint (``zu-run-contained``) executed INSIDE the
    container. Reads ``ZU_TASK`` / ``ZU_CONFIG`` (JSON) from the environment, runs
    the agent, and emits the Result + event log as one JSON object on stdout."""
    task = json.loads(os.environ.get("ZU_TASK") or "{}")
    config = json.loads(os.environ.get("ZU_CONFIG") or "{}")
    result, events = asyncio.run(_run_in_process(task, config))
    json.dump(
        {
            "result": result.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in events],
        },
        sys.stdout,
        default=str,
    )
    sys.stdout.write("\n")
    return 0


@dataclass
class SandboxLauncher:
    """Run the whole agent inside a hardened container whose sole egress is a proxy
    SIDECAR on an internal (default-DROP) network — the faithful topology
    (RED_TEAM_CONTAINER.md §3), the same one ``SidecarContainerGate`` enforces.

    A host-side proxy cannot be the sole egress of an ``--internal`` container, so
    the proxy runs as its own container on the internal network (the target's only
    route off-box) with a second leg on bridge so IT — and only it — reaches the
    outside. The target is internal-only, routed through the proxy by name, kept
    alive with ``sleep infinity`` so we exec ``zu-run-contained`` into it.

    ``backend`` is a ``LocalDockerBackend`` (its docker client manages the network
    and sidecar). ``allowlist`` on :meth:`run` is what the proxy permits — the real
    egress boundary; every other host (and every internal/metadata host) is refused
    and logged. ``"*"`` permits any host: pass an explicit list for a real boundary."""

    backend: Any
    image: str
    network_name: str = "zu-sandbox-net"
    proxy_port: int = 8080
    seccomp: str | None = None          # None -> the shipped blocking profile
    exec_timeout_s: float | None = None
    ready_timeout_s: float = 20.0

    async def run(
        self, task: dict, config: dict, *, allowlist: list[str]
    ) -> tuple[Result, list[dict]]:
        client = self.backend._docker()
        proxy_name = f"{self.network_name}-proxy"
        # Clear any resources a crashed prior run may have left behind.
        await self._remove_container(client, proxy_name)
        await self._remove_network(client, self.network_name)
        net = await asyncio.to_thread(client.networks.create, self.network_name, internal=True)
        proxy = None
        sandbox = None
        try:
            # The egress-proxy sidecar: a STABLE name (so the target resolves it via
            # the internal network's embedded DNS), on the internal network, plus a
            # bridge leg so it — and only it — reaches the outside.
            proxy_env = {
                "ZU_EGRESS_ALLOWLIST": ",".join(allowlist),
                "ZU_EGRESS_PORT": str(self.proxy_port),
            }
            # The proxy is trusted control-plane infra (the egress boundary itself),
            # run as root so it can bind/log/write regardless of the image's default
            # user. The untrusted target below keeps the image's non-root user.
            proxy = await asyncio.to_thread(
                client.containers.run, self.image, ["zu-egress-proxy"], name=proxy_name,
                network=self.network_name, environment=proxy_env, user="0", detach=True)
            bridge = await asyncio.to_thread(client.networks.get, "bridge")
            await asyncio.to_thread(bridge.connect, proxy)
            await self._await_proxy_ready(proxy)

            # The target: internal-only (the proxy is the only route off-box), caps
            # dropped + blocking seccomp, kept alive so we exec the agent into it.
            target_spec: dict = {
                "image": self.image,
                "network": "isolated",
                "network_name": self.network_name,
                "proxy": {"host": proxy_name, "port": self.proxy_port},
                "seccomp": self.seccomp or _seccomp_block_profile(),
                "command": ["sleep", "infinity"],
            }
            sandbox = await self.backend.launch(target_spec)
            # `docker exec` does not inherit the container's runtime proxy env, so
            # pass it explicitly. ZU_SANDBOXED marks the run contained — set HERE,
            # where the boundary is actually established, never baked into the image.
            proxy_url = f"http://{proxy_name}:{self.proxy_port}"
            exec_env = {
                SANDBOX_ENV: "1",
                "ZU_TASK": json.dumps(task),
                "ZU_CONFIG": json.dumps(config),
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url, "https_proxy": proxy_url,
                "NO_PROXY": "localhost,127.0.0.1",
            }
            code, out, err = await self.backend.exec_entrypoint(
                sandbox, ["zu-run-contained"],
                environment=exec_env, timeout_s=self.exec_timeout_s,
            )
            if not out.strip():
                raise RuntimeError(
                    f"contained run produced no output (exit {code}): {err[:300]}"
                )
            payload = _last_json_object(out)
            result = Result.model_validate(payload["result"])
            return result, payload.get("events", [])
        finally:
            if sandbox is not None:
                await self.backend.destroy(sandbox)
            await self._best_effort(proxy, "remove", force=True)
            await self._best_effort(net, "remove")

    async def _await_proxy_ready(self, proxy: Any) -> None:
        deadline = time.monotonic() + self.ready_timeout_s
        while time.monotonic() < deadline:
            await asyncio.to_thread(proxy.reload)
            logs = (await asyncio.to_thread(proxy.logs)).decode("utf-8", "replace")
            if "proxy.ready" in logs:
                return
            if getattr(proxy, "status", "") in ("exited", "dead"):
                raise RuntimeError(f"proxy sidecar exited before ready: {logs[-300:]}")
            await asyncio.sleep(0.2)
        raise RuntimeError("proxy sidecar did not become ready in time")

    @staticmethod
    async def _best_effort(obj: Any, method: str, **kw: Any) -> None:
        if obj is None:
            return
        try:
            await asyncio.to_thread(getattr(obj, method), **kw)
        except Exception:  # noqa: BLE001 - teardown must not raise over the result
            pass

    @staticmethod
    async def _remove_container(client: Any, name: str) -> None:
        try:
            c = await asyncio.to_thread(client.containers.get, name)
            await asyncio.to_thread(c.remove, force=True)
        except Exception:  # noqa: BLE001 - absent is the normal case
            pass

    @staticmethod
    async def _remove_network(client: Any, name: str) -> None:
        try:
            n = await asyncio.to_thread(client.networks.get, name)
            await asyncio.to_thread(n.remove)
        except Exception:  # noqa: BLE001 - absent is the normal case
            pass
