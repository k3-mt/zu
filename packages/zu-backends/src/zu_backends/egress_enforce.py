"""Pluggable egress enforcement (ZU-NET-1).

Distinct from the ``EgressProxy`` (which OBSERVES and allows): an
``EgressEnforcement`` installs the network policy that PREVENTS bypass — the
default-DROP that makes the proxy the only path off-box — AND gates DNS, which an
L3 allowlist alone would miss (the embedded resolver is a covert egress channel).
Making it a port means the mechanism is interchangeable without writing a whole
new ``SandboxBackend``: the same run can be enforced by Docker's internal network
today and by nftables or WireGuard tomorrow.

The ``spec`` dict carries::

    {"allowlist": ["api.example.com", ...],     # hosts the sandbox may reach
     "dns": "pin" | "deny" | ["resolver-ip"],   # how DNS is gated
     "proxy": {"host": ..., "ip": ..., "port": ...}}  # the sole egress leg

Three reference impls ship here:
  * ``DockerInternalNetEnforcement`` — wraps today's default (a Docker internal
    network with no default route); the no-op default so existing runs are
    unchanged.
  * ``NftablesEnforcement`` — Linux-native (`nft`), default-DROP + allow only the
    proxy + DNS deny/pin. Shells out; no third-party dependency.
  * ``ScriptedEnforcement`` — records apply/revoke; proves swappability offline.
"""

from __future__ import annotations

import subprocess
from typing import Any


class ScriptedEnforcement:
    """Records calls instead of touching the network — the offline proof that a
    ``SandboxBackend`` can drive any conformant enforcement mechanism, so a swap
    needs no core change."""

    name = "scripted"

    def __init__(self) -> None:
        self.applied: list[dict] = []
        self.revoked: list[Any] = []

    async def apply(self, spec: dict) -> Any:
        self.applied.append(spec)
        return {"handle": len(self.applied), "spec": spec}

    async def revoke(self, handle: Any) -> None:
        self.revoked.append(handle)


# A non-resolving nameserver: queries go to loopback where nothing listens, so
# name resolution fails fast. The sandbox never needs a resolver — it reaches the
# proxy by its pinned /etc/hosts IP — so this closes the embedded-resolver covert
# egress channel without breaking the one host the target must reach.
_DENY_RESOLVER = ["127.0.0.1"]


def docker_net_policy(spec: dict) -> dict:
    """Pure: derive the container network-policy kwargs the SandboxBackend merges —
    pin the proxy host to its IP in ``extra_hosts`` and gate DNS — from the run
    spec ({"proxy": {host, ip, port}, "dns": "pin"|"deny"|[resolvers]})."""
    proxy = spec.get("proxy") or {}
    dns_mode = spec.get("dns", "pin")
    policy: dict = {}
    if proxy.get("host") and proxy.get("ip"):
        policy["extra_hosts"] = {proxy["host"]: proxy["ip"]}
    if dns_mode in ("pin", "deny"):
        policy["dns"] = list(_DENY_RESOLVER)
    elif isinstance(dns_mode, list):
        policy["dns"] = list(dns_mode)
    return policy


class DockerInternalNetEnforcement:
    """The default reference mechanism: a Docker internal network (no default
    route) is the default-DROP, the proxy bridges out, and ``apply`` returns the
    network-policy kwargs — the proxy IP pin + DNS gate — the ``LocalDockerBackend``
    merges into the container spec. This is the seam the sandbox launcher routes
    through, so the *mechanism* is swappable (nftables/WireGuard) with no core
    change (ZU-NET-1)."""

    name = "docker-internal-net"

    def __init__(self) -> None:
        self._applied: list[dict] = []

    async def apply(self, spec: dict) -> Any:
        self._applied.append(spec)
        return {"mechanism": "docker-internal-net", "policy": docker_net_policy(spec), "spec": spec}

    async def revoke(self, handle: Any) -> None:
        return None


class NftablesEnforcement:
    """Linux-native enforcement via ``nft``: a per-run table that DROPs all egress
    except to the proxy IP, and denies DNS (the sandbox resolves nothing; the
    proxy host is pinned by IP). Requires Linux + privilege; a no-op contract on
    other platforms is intentionally NOT provided — fail loudly if misconfigured.
    """

    name = "nftables"

    def __init__(self, table: str = "zu_egress") -> None:
        self._table = table

    def _nft(self, *args: str) -> None:
        subprocess.run(["nft", *args], check=True, capture_output=True)

    async def apply(self, spec: dict) -> Any:
        proxy = spec.get("proxy", {})
        proxy_ip = proxy.get("ip")
        if not proxy_ip:
            raise ValueError("nftables enforcement requires spec['proxy']['ip']")
        t = self._table
        # Fresh table; default policy DROP; allow established + the proxy only.
        self._nft("add", "table", "inet", t)
        self._nft(
            "add", "chain", "inet", t, "output",
            "{ type filter hook output priority 0 ; policy drop ; }",
        )
        self._nft("add", "rule", "inet", t, "output", "ct", "state", "established,related", "accept")
        self._nft("add", "rule", "inet", t, "output", "ip", "daddr", str(proxy_ip), "accept")
        # DNS gating: with dns != a resolver list, the sandbox gets no resolver
        # (the default-DROP already blocks 53); the proxy host is reached by pinned
        # IP, so name resolution is unnecessary and the covert channel is closed.
        return {"mechanism": "nftables", "table": t}

    async def revoke(self, handle: Any) -> None:
        try:
            self._nft("delete", "table", "inet", self._table)
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


__all__ = ["ScriptedEnforcement", "DockerInternalNetEnforcement", "NftablesEnforcement"]
