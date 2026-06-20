"""SidecarContainerGate — the programmatic gate on the faithful sidecar topology.

`DockerContainerRunner` (P1) routes the target through a *host-side* proxy: fine
for the runner+observer flow, but a host proxy cannot be the *sole* egress of an
``--internal`` container, so it does not itself enforce default-DROP. This runs the
real topology end to end (RED_TEAM_CONTAINER.md §3):

    an egress-proxy SIDECAR on an internal network is the target's only route
    off-box; the target execs the runner; the verdict rests on the proxy's
    connection log read via ``docker logs`` — a record the target cannot author.

That is what makes the programmatic gate *enforce*, not merely observe. It is
live-only (needs Docker); the merge/judge is the same code the scripted pipeline
uses, so the verdict layer is unchanged. The proxy log parsing is a pure function,
unit-tested; the orchestration is exercised by the opt-in live test.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from .container import ContainerResult, merge_evidence
from .runner import jsonl_to_events
from .verdict import NeighbourHealth, ObservedRun, default_observers


def parse_proxy_log(text: str) -> list[dict]:
    """Connection records from a sidecar proxy's stdout (one JSONL line each).
    Skips the ``proxy.ready`` banner and any non-JSON noise; a connection record
    is any JSON object carrying a ``host`` key."""
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and "host" in rec:
            out.append(rec)
    return out


@dataclass
class SidecarContainerGate:
    """Run a scenario spec in a container whose sole egress is a proxy sidecar,
    and judge it on the proxy's out-of-band connection log.

    ``backend`` is a ``LocalDockerBackend`` (used for the target's lifecycle,
    ``exec_entrypoint``, ``fs_diff``, and its docker client). ``allowlist`` is what
    the proxy enforces; ``mitm`` enables TLS interception (the proxy mints a per-run
    CA into a shared volume the target trusts). ``host_monitor`` (optional) collects
    fs/process effects."""

    backend: Any
    image: str
    allowlist: list[str]
    mitm: bool = False
    network_name: str = "zu-rt-gate"
    host_monitor: Any = None
    seccomp_profile: str | None = None  # applied to the target (P3 syscall audit)
    proxy_port: int = 8080
    ready_timeout_s: float = 20.0
    log_settle_s: float = 1.0  # let a forwarded connection flush before reading logs

    async def run(self, spec: dict) -> ContainerResult:
        client = self.backend._docker()
        proxy_name = f"{self.network_name}-proxy"
        # Clear any stale resources a crashed prior run may have left behind.
        await self._remove_container(client, proxy_name)
        await self._remove_network(client, self.network_name)
        net = await asyncio.to_thread(
            client.networks.create, self.network_name, internal=True)
        vol = None
        proxy = None
        sandbox = None
        host_effects: list[dict] = []
        try:
            env = {"ZU_EGRESS_ALLOWLIST": ",".join(self.allowlist),
                   "ZU_EGRESS_PORT": str(self.proxy_port)}
            proxy_volumes: dict = {}
            if self.mitm:
                vol = await asyncio.to_thread(client.volumes.create)
                env.update({"ZU_EGRESS_MITM": "1", "ZU_EGRESS_CA_OUT": "/ca/ca.pem"})
                proxy_volumes = {vol.name: {"bind": "/ca", "mode": "rw"}}
            # The proxy sidecar: a STABLE name (so the target resolves it via the
            # internal network's embedded DNS — an auto-generated name does not
            # resolve reliably), on the internal network, plus a second leg on
            # bridge so IT — and only it — bridges out.
            # The proxy is trusted control-plane infra (it IS the egress boundary),
            # run as root so it can write the per-run MITM CA into the shared volume
            # even when the image's default user is unprivileged. The untrusted
            # TARGET below keeps the image's non-root user.
            proxy = await asyncio.to_thread(
                client.containers.run, self.image, ["zu-egress-proxy"], name=proxy_name,
                network=self.network_name, environment=env, volumes=proxy_volumes,
                user="0", detach=True)
            bridge = await asyncio.to_thread(client.networks.get, "bridge")
            await asyncio.to_thread(bridge.connect, proxy)
            await self._await_proxy_ready(proxy)

            # The target: internal-only (no route but the proxy), routed through it,
            # kept alive by `sleep infinity` so we can exec the runner into it.
            target_spec: dict = {
                "image": self.image, "network": "isolated", "network_name": self.network_name,
                "proxy": {"host": proxy_name, "port": self.proxy_port},
                "command": ["sleep", "infinity"],
            }
            if self.mitm and vol is not None:
                target_spec["ca_volume"] = vol.name
            if self.seccomp_profile:
                target_spec["seccomp"] = self.seccomp_profile
            sandbox = await self.backend.launch(target_spec)
            # `docker exec` does NOT inherit the container's runtime proxy env, so
            # pass it explicitly — otherwise the runner's tools egress directly and
            # hit the internal network's dead end instead of routing through the proxy.
            proxy_url = f"http://{proxy_name}:{self.proxy_port}"
            exec_env = {
                "ZU_REDTEAM_SPEC": json.dumps(spec),
                "HTTP_PROXY": proxy_url, "HTTPS_PROXY": proxy_url,
                "http_proxy": proxy_url, "https_proxy": proxy_url,
                "NO_PROXY": "localhost,127.0.0.1",
            }
            if self.mitm:
                exec_env["SSL_CERT_FILE"] = "/ca/ca.pem"
                exec_env["REQUESTS_CA_BUNDLE"] = "/ca/ca.pem"
            code, out, err = await self.backend.exec_entrypoint(
                sandbox, ["zu-redteam-run"], environment=exec_env)
            if not out.strip():
                raise RuntimeError(f"in-container runner produced no events (exit {code}): {err[:300]}")
            events = jsonl_to_events(out)
            if self.host_monitor is not None:
                host_effects = await self.host_monitor.collect(sandbox, self.backend)
            # A forwarded connection is logged in the proxy's per-connection finally,
            # which can land just after the in-container request returns; let it
            # settle so the connection record is flushed before we read the log.
            await asyncio.sleep(self.log_settle_s)
            await asyncio.to_thread(proxy.reload)
            logs = await asyncio.to_thread(proxy.logs)
            connections = parse_proxy_log(logs.decode("utf-8", "replace"))
        finally:
            if sandbox is not None:
                await self.backend.destroy(sandbox)
            await self._best_effort(proxy, "remove", force=True)
            await self._best_effort(vol, "remove", force=True)
            await self._best_effort(net, "remove")

        run = ObservedRun.from_events(events, None, planted_secret=spec.get("planted_secret", ""))
        merged = merge_evidence(run, connections, host_effects)
        observers = [*default_observers(), NeighbourHealth(spec.get("neighbours") or [])]
        breaches = [b for o in observers if (b := o.inspect(merged)) is not None]
        return ContainerResult(
            observed=merged, breaches=breaches,
            connections=list(connections), host_effects=list(host_effects))

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
