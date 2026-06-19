"""LocalEgressProxy — the real egress proxy for the red-team container form (P1).

This is the *enforcement* the in-process gate can only observe: a forward proxy
that is the target container's **sole route off-box**, so the verdict rests on a
record the target cannot author (RED_TEAM_CONTAINER.md §3.1). It does two jobs:

* **Logs** every connection the target makes — host, port, scheme, bytes — as a
  structured record. This, not the tool's self-reported observation, is what the
  ``EgressBreach``/``ExfilBreach`` observers read in the container form.
* **Enforces** the declared allowlist: a CONNECT/request to a host outside the
  allowlist (or to an internal/metadata host, ever) is **refused** and logged with
  ``allowed: false``. A benign plugin has no business reaching off-allowlist, so
  the refused *attempt* is itself the breach.

It implements the ``EgressProxy`` port (``launch``/``connections``/``close``), so
``ContainerGate`` drives it exactly like the scripted stand-in — the P0 pipeline
becomes the P1 pipeline by swapping this in. Pure stdlib asyncio: no Docker, no
optional dependency, and unit-testable over loopback.

Scope note: the proxy is the only egress *path*, but the hard guarantee that a
tool cannot bypass it (open a raw socket directly) is the container's network
policy (default-DROP), configured by the ``SandboxBackend`` — not this process.
The proxy is where egress is *seen and allowed*; the network policy is where
bypass is *prevented*. Both are needed; this is the former.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# The egress allowlist sentinel (mirrors zu_core.ports.EGRESS_OPEN). Kept as a
# literal so this stdlib-only module needs no import for one constant.
_EGRESS_OPEN = "*"
_PROXY_ERROR_CODES = {"refused": b"HTTP/1.1 403 Forbidden\r\n\r\n",
                      "upstream": b"HTTP/1.1 502 Bad Gateway\r\n\r\n"}


def _is_internal_host(host: str) -> bool:
    """A host no plugin may ever reach: loopback / private / link-local (cloud
    metadata 169.254.169.254) or the well-known internal names. A literal IP is
    decided structurally; a name only by the known internal spellings (we do not
    resolve names here — that is the DNS-pin's job in the backend)."""
    lowered = (host or "").lower()
    if lowered in {"localhost", "metadata.google.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved


@dataclass
class _ProxyHandle:
    """Live handle to a running proxy: its address, the asyncio server, and the
    connection log accumulated this run."""

    host: str
    port: int
    server: asyncio.AbstractServer
    log: list[dict]
    allow: set[str]


@dataclass
class LocalEgressProxy:
    """A CONNECT + absolute-form HTTP forward proxy that logs and allowlist-gates
    egress. ``block_internal`` is the SSRF guard (refuse loopback/private/metadata
    even if somehow allowlisted); disable it only in loopback tests."""

    name = "local-egress-proxy"
    bind_host: str = "127.0.0.1"
    bind_port: int = 0  # 0 -> an ephemeral port the OS assigns
    block_internal: bool = True
    # Per-tunnel idle/copy bound so a wedged upstream can't hang the run forever.
    io_timeout_s: float = 30.0
    # P2 TLS MITM: a MitmCA enables decrypting HTTPS to record the request URL/body
    # (so ExfilBreach can see a secret in an HTTPS query). None -> blind CONNECT
    # tunnel (P1): the host is logged, the payload is not. ``upstream_ssl`` overrides
    # the context used to re-originate TLS upstream (tests inject an unverified one).
    mitm: Any = None
    upstream_ssl: Any = None
    # Cap on the request body captured for the exfil log (bytes).
    body_cap: int = 65536
    # Optional callback(entry: dict) invoked once per finished connection — the
    # sidecar CLI uses it to stream the connection log as JSONL on stdout.
    on_connection: Any = None

    async def launch(self, spec: dict) -> _ProxyHandle:
        """Start the proxy for one run against the union allowlist in
        ``spec['allowlist']`` (``['*']`` permits any host). Returns a handle
        carrying the bound ``{host, port}`` the container routes through."""
        allow = set(spec.get("allowlist") or [])
        log: list[dict] = []

        async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._serve(reader, writer, allow, log)

        server = await asyncio.start_server(on_client, self.bind_host, self.bind_port)
        sock = server.sockets[0].getsockname()
        return _ProxyHandle(host=sock[0], port=sock[1], server=server, log=log, allow=allow)

    def connections(self, handle: _ProxyHandle) -> list[dict]:
        return [dict(c) for c in handle.log]

    async def close(self, handle: _ProxyHandle) -> None:
        handle.server.close()
        try:
            await handle.server.wait_closed()
        except Exception:  # noqa: BLE001 - teardown must not raise over the result
            pass

    # --- connection handling ---------------------------------------------

    def _allowed(self, host: str, allow: set[str]) -> bool:
        if self.block_internal and _is_internal_host(host):
            return False
        if _EGRESS_OPEN in allow:
            return True
        return host in allow

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        allow: set[str], log: list[dict],
    ) -> None:
        peer = writer.get_extra_info("peername")
        client = f"{peer[0]}:{peer[1]}" if peer else "?"
        entry: dict | None = None
        try:
            header_block, request_line = await self._read_headers(reader)
            if not request_line:
                return
            method, target, _, _rest = (request_line.decode("latin1") + "  ").split(" ", 3)
            method = method.upper()
            if method == "CONNECT":
                host, _, port_s = target.partition(":")
                port, scheme = (int(port_s) if port_s.isdigit() else 443), "https"
            else:  # absolute-form: METHOD http://host[:port]/path HTTP/1.1
                parts = urlsplit(target)
                host, port = parts.hostname or "", parts.port or 80
                scheme = parts.scheme or "http"
            entry = {"client": client, "host": host, "port": port, "scheme": scheme,
                     "bytes_out": 0, "allowed": False}
            log.append(entry)

            if not host or not self._allowed(host, allow):
                writer.write(_PROXY_ERROR_CODES["refused"])
                await writer.drain()
                return
            entry["allowed"] = True
            if method == "CONNECT" and self.mitm is not None:
                await self._mitm_forward(reader, writer, host, port, entry)
            else:
                await self._forward(reader, writer, host, port, method, target,
                                    header_block, request_line, entry)
        except Exception:  # noqa: BLE001 - a proxy hiccup is an observation, not a crash
            try:
                writer.write(_PROXY_ERROR_CODES["upstream"])
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
        finally:
            # Stream the finalised connection record (used by the sidecar CLI to
            # emit one JSONL line per connection on stdout, which the host control
            # plane reads via `docker logs`).
            if entry is not None and self.on_connection is not None:
                try:
                    self.on_connection(entry)
                except Exception:  # noqa: BLE001 - a logging hook must never break the proxy
                    pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _read_headers(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        """Read the request line + headers up to the blank line, returning the raw
        block and the request line. Bounded so a client cannot stream headers
        forever."""
        request_line = await asyncio.wait_for(reader.readline(), self.io_timeout_s)
        block = request_line
        while True:
            line = await asyncio.wait_for(reader.readline(), self.io_timeout_s)
            block += line
            if line in (b"\r\n", b"\n", b""):
                break
        return block, request_line

    async def _forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        host: str, port: int, method: str, target: str,
        header_block: bytes, request_line: bytes, entry: dict,
    ) -> None:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), self.io_timeout_s)
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                # Rewrite absolute-form to origin-form and forward the request
                # (headers + any body the client sends next) to the upstream.
                parts = urlsplit(target)
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                rest = header_block[len(request_line):]
                new_line = f"{method} {path} HTTP/1.1\r\n".encode("latin1")
                up_writer.write(new_line + rest)
                await up_writer.drain()
                entry["bytes_out"] += len(new_line) + len(rest)
            await self._pump(reader, up_reader, writer, up_writer, entry)
        finally:
            up_writer.close()
            try:
                await up_writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    def _upstream_ssl(self) -> Any:
        import ssl

        return self.upstream_ssl if self.upstream_ssl is not None else ssl.create_default_context()

    async def _read_bounded_body(self, reader: asyncio.StreamReader, header_block: bytes) -> bytes:
        """Read up to ``body_cap`` bytes of the request body (when Content-Length
        says there is one), so a secret smuggled into a POST body — not just a query
        string — lands in the exfil log. Best-effort: a short/absent body is fine."""
        length = 0
        for line in header_block.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    length = 0
        if length <= 0:
            return b""
        try:
            return await asyncio.wait_for(reader.readexactly(min(length, self.body_cap)), self.io_timeout_s)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            return b""

    async def _mitm_forward(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
        host: str, port: int, entry: dict,
    ) -> None:
        """TLS MITM (P2): become the client's TLS server with a minted leaf, read
        the decrypted request (recording its URL/body into the connection log for
        ``ExfilBreach``), then re-originate TLS to the real upstream and pump the
        response back. The exfil record is written BEFORE the upstream hop, so even
        an unreachable upstream cannot hide a secret the client tried to send."""
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        # Impersonate the upstream to the in-container client.
        await writer.start_tls(self.mitm.leaf_context(host))
        header_block, request_line = await self._read_headers(reader)
        try:
            method, path, _ = (request_line.decode("latin1") + "  ").split(" ", 2)
        except ValueError:
            path = "/"
        entry["url"] = f"https://{host}{path.strip()}"
        body = await self._read_bounded_body(reader, header_block)
        if body:
            entry["body"] = body.decode("latin1", "replace")[: self.body_cap]
        entry["bytes_out"] += len(header_block) + len(body)
        # Re-originate TLS upstream and pump the response (re-encrypted to client).
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=self._upstream_ssl(), server_hostname=host),
            self.io_timeout_s)
        try:
            up_writer.write(header_block + body)
            await up_writer.drain()
            await self._pump(reader, up_reader, writer, up_writer, entry)
        finally:
            up_writer.close()
            try:
                await up_writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _pump(
        self, c_reader: asyncio.StreamReader, u_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter, u_writer: asyncio.StreamWriter, entry: dict,
    ) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter, count: bool) -> None:
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    if count:
                        entry["bytes_out"] += len(chunk)
                    dst.write(chunk)
                    await dst.drain()
            except Exception:  # noqa: BLE001 - either side closing ends the copy
                pass
            finally:
                try:
                    dst.write_eof()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.wait(
            {asyncio.create_task(copy(c_reader, u_writer, True)),
             asyncio.create_task(copy(u_reader, c_writer, False))},
            timeout=self.io_timeout_s,
        )


def main(argv: list[str] | None = None) -> int:
    """``zu-egress-proxy`` — run the proxy as a sidecar container, the target's sole
    route off-box (RED_TEAM_CONTAINER.md §3.1). Each finished connection is printed
    as one JSONL line on stdout, which the host control plane reads via
    ``docker logs``. Config via env:

      ZU_EGRESS_ALLOWLIST  comma-separated hosts (``*`` = open)   [default ``*``]
      ZU_EGRESS_BIND       bind address                          [default 0.0.0.0]
      ZU_EGRESS_PORT       bind port                             [default 8080]
      ZU_EGRESS_MITM       ``1`` -> TLS MITM (decrypt HTTPS to log URL/body)  [off]
      ZU_EGRESS_CA_OUT     path to write the per-run CA cert PEM (so the target
                           can trust it); only used when MITM is on
    """
    import json
    import os

    allow = [h for h in (os.environ.get("ZU_EGRESS_ALLOWLIST", "*")).split(",") if h]
    bind = os.environ.get("ZU_EGRESS_BIND", "0.0.0.0")
    port = int(os.environ.get("ZU_EGRESS_PORT", "8080"))

    mitm = None
    if os.environ.get("ZU_EGRESS_MITM") == "1":
        from .mitm import MitmCA

        mitm = MitmCA()
        ca_out = os.environ.get("ZU_EGRESS_CA_OUT")
        if ca_out:
            with open(ca_out, "wb") as fh:
                fh.write(mitm.ca_cert_pem())

    def emit(entry: dict) -> None:
        print(json.dumps(entry), flush=True)

    proxy = LocalEgressProxy(bind_host=bind, bind_port=port, on_connection=emit, mitm=mitm)

    async def serve() -> None:
        await proxy.launch({"allowlist": allow})
        print(json.dumps({"event": "proxy.ready", "bind": bind, "port": port,
                          "allowlist": allow, "mitm": mitm is not None}), flush=True)
        await asyncio.Event().wait()  # run until the container is stopped

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:  # pragma: no cover - container stop
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entry
    raise SystemExit(main())
