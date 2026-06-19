"""LocalEgressProxy — the real egress proxy (RED_TEAM_CONTAINER.md P1), exercised
over loopback with no Docker. The two behaviours that matter: it REFUSES an
off-allowlist host (and logs the attempt), and it TUNNELS an allowed host while
logging it. Both are the ground truth the verdict observers read."""

from __future__ import annotations

import asyncio

from zu_backends.egress_proxy import LocalEgressProxy


async def _connect_request(host: str, port: int, line: bytes) -> tuple[bytes, asyncio.StreamWriter]:
    """Open a raw connection to the proxy, send one request line + blank line, and
    read the proxy's status line back."""
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(line + b"\r\n\r\n")
    await writer.drain()
    status = await asyncio.wait_for(reader.readline(), 5)
    return status, writer


async def test_refuses_off_allowlist_host_and_logs_the_attempt() -> None:
    proxy = LocalEgressProxy()
    handle = await proxy.launch({"allowlist": ["allowed.example"]})
    try:
        status, writer = await _connect_request(
            handle.host, handle.port, b"CONNECT evil.example:443 HTTP/1.1")
        assert b"403" in status  # refused before any upstream connection
        writer.close()
        # Give the handler a tick to append its log entry.
        await asyncio.sleep(0.05)
        conns = proxy.connections(handle)
        assert conns and conns[0]["host"] == "evil.example"
        assert conns[0]["allowed"] is False
        assert conns[0]["scheme"] == "https" and conns[0]["port"] == 443
    finally:
        await proxy.close(handle)


async def test_refuses_internal_host_even_if_allowlisted() -> None:
    # The SSRF guard is unconditional: an internal/metadata host is refused even
    # if it slipped onto the allowlist.
    proxy = LocalEgressProxy()  # block_internal=True (default)
    handle = await proxy.launch({"allowlist": ["169.254.169.254", "*"]})
    try:
        status, writer = await _connect_request(
            handle.host, handle.port, b"CONNECT 169.254.169.254:80 HTTP/1.1")
        assert b"403" in status
        writer.close()
        await asyncio.sleep(0.05)
        assert proxy.connections(handle)[0]["allowed"] is False
    finally:
        await proxy.close(handle)


async def test_tunnels_an_allowed_host_and_logs_bytes() -> None:
    # Stand up a loopback echo upstream; allow it by host:port. block_internal is
    # off so the loopback upstream isn't SSRF-refused (loopback is "internal").
    echo_chunks: list[bytes] = []

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        echo_chunks.append(data)
        writer.write(b"PONG:" + data)
        await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    up_host, up_port = upstream.sockets[0].getsockname()[:2]

    proxy = LocalEgressProxy(block_internal=False)
    handle = await proxy.launch({"allowlist": [up_host]})
    try:
        reader, writer = await asyncio.open_connection(handle.host, handle.port)
        writer.write(f"CONNECT {up_host}:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        established = await asyncio.wait_for(reader.readline(), 5)
        assert b"200" in established  # tunnel established
        await reader.readline()  # consume the blank line after the status

        writer.write(b"PING")
        await writer.drain()
        echoed = await asyncio.wait_for(reader.read(64), 5)
        assert b"PONG:PING" in echoed
        writer.close()
        await asyncio.sleep(0.05)

        conn = proxy.connections(handle)[0]
        assert conn["allowed"] is True and conn["host"] == up_host
        assert conn["bytes_out"] >= 4  # at least the "PING" we sent upstream
    finally:
        await proxy.close(handle)
        upstream.close()


async def test_open_egress_allows_any_host() -> None:
    proxy = LocalEgressProxy()
    handle = await proxy.launch({"allowlist": ["*"]})
    try:
        # A public host is permitted under EGRESS_OPEN; we only check the verdict
        # the proxy reached (no real upstream needed — the connect will fail fast
        # and surface as 502, but the allow decision is logged first).
        status, writer = await _connect_request(
            handle.host, handle.port, b"CONNECT public.example:443 HTTP/1.1")
        writer.close()
        await asyncio.sleep(0.05)
        conn = proxy.connections(handle)[0]
        assert conn["allowed"] is True and conn["host"] == "public.example"
        assert status  # some HTTP status came back (200 if reachable, 502 if not)
    finally:
        await proxy.close(handle)


# --- the host-effect monitor (RED_TEAM_CONTAINER.md P3) ---------------------


class _DiffBackend:
    def __init__(self, diffs):
        self._d = diffs

    async def fs_diff(self, sandbox):
        return self._d


async def test_fs_diff_monitor_flags_out_of_scope_writes_only() -> None:
    from zu_backends.host_monitor import DockerFsDiffMonitor

    backend = _DiffBackend([
        {"path": "/tmp/cache", "kind": "added"},       # writable scope -> ignored
        {"path": "/run/x", "kind": "added"},           # writable scope -> ignored
        {"path": "/etc/cron.d/payload", "kind": "added"},  # out of scope -> flagged
    ])
    effects = await DockerFsDiffMonitor().collect(sandbox=None, backend=backend)
    paths = [e["path"] for e in effects]
    assert paths == ["/etc/cron.d/payload"]
    assert effects[0]["kind"] == "fs:write"


async def test_scripted_host_monitor_replays_its_effects() -> None:
    from zu_backends.scripted_sandbox import ScriptedHostMonitor

    mon = ScriptedHostMonitor(effects=[{"kind": "subprocess", "argv": ["sh", "-c", "x"]}])
    effects = await mon.collect()
    assert effects[0]["argv"] == ["sh", "-c", "x"]
