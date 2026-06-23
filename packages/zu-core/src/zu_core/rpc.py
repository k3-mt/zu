"""The thin out-of-process plugin boundary (ZU-NET-3).

In-process plugins share the harness's address space: any plugin's code can read
any secret the harness holds (``os.environ``, attribute access, ``gc``). For a
credential broker that is fatal — a harness compromise would leak the card
number. The fix is a **separate trust domain**: the plugin runs in its own
process (ideally its own uid), and the harness talks to it over a typed channel.
Then the broker's secret lives only in the broker's memory; a harness compromise
can ask the broker to *use* the credential (mint a token) but cannot exfiltrate
it.

This module is the **trusted wire contract** the loop holds — deliberately tiny:
a length-prefixed JSON frame codec, a sequential ``RpcClient``, forwarding
proxies that satisfy an existing port by forwarding each call over the socket
(``RemoteTool`` → :class:`~zu_core.ports.Tool`, ``RemoteChannel`` →
:class:`~zu_core.ports.Channel`), and a generic ``serve`` loop the worker runs.
No auth, no TLS, no reconnection: the socket is local and the *launcher* (the
subprocess/uid machinery, in ``zu-backends``) owns lifecycle — keeping this in
core stdlib-only and small. The launcher is the part that needs privilege; this
is the part that needs trust.
"""

from __future__ import annotations

import asyncio
import json
import struct
from collections.abc import Awaitable, Callable
from typing import Any

from .ports import ChannelRequest, ChannelResponse

# 4-byte big-endian length prefix + utf-8 JSON body. A frame never spans reads
# ambiguously: read exactly 4 bytes, then exactly that many.
_LEN = struct.Struct(">I")
_MAX_FRAME = 16 * 1024 * 1024  # 16 MiB ceiling — a frame larger than this is refused


def _encode_frame(obj: dict) -> bytes:
    body = json.dumps(obj, default=str).encode("utf-8")
    if len(body) > _MAX_FRAME:
        raise ValueError(f"rpc frame too large: {len(body)} bytes")
    return _LEN.pack(len(body)) + body


async def _read_frame(reader: asyncio.StreamReader) -> dict | None:
    header = await reader.readexactly(_LEN.size)
    (n,) = _LEN.unpack(header)
    if n > _MAX_FRAME:
        raise ValueError(f"rpc frame too large: {n} bytes")
    body = await reader.readexactly(n)
    obj: dict = json.loads(body.decode("utf-8"))
    return obj


class RpcClient:
    """A sequential request/response client over a unix socket. One call in
    flight at a time (a lock serialises), which is all a single tool/broker
    invocation needs and keeps the contract trivially correct."""

    def __init__(self, sock_path: str) -> None:
        self._sock_path = sock_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def _ensure(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._reader is None or self._writer is None:
            self._reader, self._writer = await asyncio.open_unix_connection(self._sock_path)
        return self._reader, self._writer

    async def call(self, method: str, args: dict) -> dict:
        async with self._lock:
            reader, writer = await self._ensure()
            self._next_id += 1
            writer.write(_encode_frame({"id": self._next_id, "method": method, "args": args}))
            await writer.drain()
            resp = await _read_frame(reader)
            if resp is None:
                raise ConnectionError("rpc worker closed the connection")
            if not resp.get("ok", False):
                raise RuntimeError(f"rpc error: {resp.get('error')}")
            result: dict = resp.get("result", {})
            return result

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001 - close is best-effort
                pass
            self._reader = self._writer = None


class RemoteTool:
    """A :class:`~zu_core.ports.Tool` that forwards each call to an
    out-of-process worker. Drop-in for the loop, which calls ``tool(ctx, **args)``
    exactly as for an in-process tool — the ``ctx`` is NOT serialised (the worker
    cannot read the harness's run state); only the call args cross the boundary."""

    def __init__(self, client: RpcClient, spec: dict) -> None:
        self.name = spec["name"]
        self.schema = spec.get("schema", {})
        self.tier = int(spec.get("tier", 1))
        self.prompt_fragment = spec.get("prompt_fragment", "")
        self.capabilities = frozenset(spec.get("capabilities", ()))
        self.egress = frozenset(spec.get("egress", ()))
        self._client = client

    async def __call__(self, ctx: Any, **kwargs: Any) -> dict:
        return await self._client.call("invoke", {"args": kwargs})


class RemoteChannel:
    """A :class:`~zu_core.ports.Channel` that forwards verbs to an out-of-process
    endpoint (e.g. the credential broker). The harness holds only this proxy and
    the socket; the channel's credential lives in the worker's address space."""

    def __init__(self, client: RpcClient, endpoint: str) -> None:
        self.endpoint = endpoint
        self._client = client

    async def call(self, req: ChannelRequest) -> ChannelResponse:
        result = await self._client.call("call", {"op": req.op, "args": req.args})
        return ChannelResponse(**result)


Handler = Callable[[str, dict], "Awaitable[dict] | dict"]


async def serve(sock_path: str, handler: Handler) -> None:
    """Serve the RPC protocol on ``sock_path`` until cancelled. ``handler(method,
    args) -> dict`` is the worker's dispatch into the real plugin. Runs in the
    worker process (the secret-bearing trust domain), not the harness."""
    async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    req = await _read_frame(reader)
                except asyncio.IncompleteReadError:
                    return  # client hung up
                if req is None:
                    return
                rid = req.get("id")
                try:
                    out = handler(req.get("method", ""), req.get("args", {}))
                    if asyncio.iscoroutine(out):
                        out = await out
                    writer.write(_encode_frame({"id": rid, "ok": True, "result": out}))
                except Exception as exc:  # noqa: BLE001 - report the error over the wire
                    writer.write(_encode_frame({"id": rid, "ok": False, "error": str(exc)}))
                await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(_on_client, path=sock_path)
    async with server:
        await server.serve_forever()
