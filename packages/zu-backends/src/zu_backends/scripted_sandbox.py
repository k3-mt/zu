"""Daemon-free stand-ins for the red-team container form (RED_TEAM_CONTAINER.md, P0).

A real container `SandboxBackend` and an `EgressProxy` need Docker and a running
proxy; these replay saved logs instead, so the whole collect→merge→judge pipeline
runs in CI with no daemon — the same way `ScriptedProvider` stands in for a live
model and the scripted sandbox stands in for the browser.

The split is deliberate: in P0 the *target* still runs on real Zu (the
`ContainerGate` runs the scenario in-process), and only the **out-of-band facts** a
real egress proxy / host-effect monitor would have observed are replayed here.
That is exactly the boundary that matters — the verdict rests on a record produced
*outside* the tool, which a deceptive tool cannot author, whether that record came
from a real proxy (P1) or this replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zu_core.ports import ToolCall


@dataclass
class _ProxyHandle:
    """Opaque handle returned by `ScriptedEgressProxy.launch` — carries the
    proxy's address and the connection log this run will report."""

    host: str
    port: int
    connections: list[dict]
    allowlist: list[str] = field(default_factory=list)


@dataclass
class ScriptedEgressProxy:
    """An `EgressProxy` (see `zu_core.ports`) that replays a fixed connection log —
    the facts a real proxy would have observed for a scenario. Each entry is a dict
    ``{client, host, ip, port, scheme, bytes_out, allowed, source?, url?}``.

    Because these facts are produced *outside* the target, pairing this with a tool
    that lies in its own observation is what demonstrates the in-process gap: the
    judge reads the proxy's record, not the tool's self-report."""

    name = "scripted-egress-proxy"
    log: list[dict] = field(default_factory=list)
    host: str = "proxy.local"
    port: int = 8080

    async def launch(self, spec: dict) -> _ProxyHandle:
        return _ProxyHandle(
            host=self.host, port=self.port,
            connections=[dict(c) for c in self.log],
            allowlist=list(spec.get("allowlist") or []),
        )

    def connections(self, handle: _ProxyHandle) -> list[dict]:
        return [dict(c) for c in handle.connections]

    async def close(self, handle: _ProxyHandle) -> None:
        return None


@dataclass
class ScriptedHostMonitor:
    """A ``HostEffectMonitor`` (see ``zu_core.ports``) that replays a fixed list of
    host-effect facts — ``{kind, path|argv, pid?}`` — the audit log a real monitor
    would have produced. Pairing it with a tool that declares no host effect is how
    the P0/P1 pipeline proves an *observed* undeclared write/spawn is caught,
    deterministically and with no daemon."""

    name = "scripted-host-monitor"
    effects: list[dict] = field(default_factory=list)

    async def collect(self, sandbox: Any = None, backend: Any = None) -> list[dict]:
        return [dict(e) for e in self.effects]


@dataclass
class ScriptedSandbox:
    """A `SandboxBackend` stand-in that replays a saved in-container event log,
    modelling "the container ran and produced these events" with no daemon. For P0
    the `ContainerGate` usually runs the target in-process on real Zu instead; this
    is for fully-frozen fixtures where even the run is replayed (a recorded breach
    promoted to a deterministic regression). Returns plain `zu_core` event objects,
    so it carries no red-team types and stays a pure infrastructure adapter."""

    name = "scripted-sandbox"
    saved_events: list[Any] = field(default_factory=list)

    async def launch(self, spec: dict) -> "ScriptedSandbox":
        return self

    async def exec(self, sandbox: Any, call: ToolCall) -> dict:
        # The replay form does not execute calls; it returns a benign empty
        # observation so the adapter still satisfies the SandboxBackend shape.
        return {"status": 200, "html": "", "url": call.args.get("url")}

    async def destroy(self, sandbox: Any) -> None:
        return None

    def events(self) -> list[Any]:
        """The saved in-container event log this sandbox replays."""
        return list(self.saved_events)
