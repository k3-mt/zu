"""The out-of-process plugin launcher (ZU-NET-3).

The privileged half of the OOP boundary: it spawns the worker subprocess (and,
when started with privilege, drops it to a separate uid), waits for its unix
socket, and returns a forwarding proxy (``RemoteTool`` / ``RemoteChannel`` from
``zu_core.rpc``) the harness holds. The wire CONTRACT and the proxies are trusted
core; this lifecycle/subprocess machinery is a plugin in ``zu-backends`` — the
same split as the ``SandboxBackend`` port (core) vs ``LocalDockerBackend``
(backends). The secret is passed in the child's environment and never written to
the parent's, so the harness process never holds it.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import uuid

from zu_core.rpc import RemoteChannel, RemoteTool, RpcClient


class OutOfProcessLauncher:
    name = "oop"

    def __init__(self) -> None:
        self._procs: list[subprocess.Popen] = []
        self._clients: list[RpcClient] = []
        # Unix socket paths are length-limited (~104 chars on macOS), and the
        # default temp dir can be long — prefer the short ``/tmp`` when present.
        base = "/tmp" if os.path.isdir("/tmp") else None
        self._dir = tempfile.mkdtemp(prefix="zuoop", dir=base)

    def _sock_path(self) -> str:
        # A short, unique path under the temp dir (unix socket paths are length
        # limited, so keep it short rather than embedding the full target ref).
        return os.path.join(self._dir, f"{uuid.uuid4().hex[:12]}.sock")

    async def _spawn(
        self,
        target_ref: str,
        kind: str,
        *,
        env: dict[str, str] | None = None,
        args_json: str = "{}",
        user: int | None = None,
    ) -> RpcClient:
        sock = self._sock_path()
        # The child env = the parent's PATH/venv plus the OOP config and any
        # plugin secret the caller passes. The secret is NOT written to the
        # parent's os.environ, so the harness process never holds it.
        child_env = dict(os.environ)
        child_env.update(env or {})
        # Tell the worker exactly which keys ARE the caller-supplied secret so it
        # can SCRUB them from its own environ after consuming them (issue #49) —
        # generic, not tied to any specific credential name. The worker deletes
        # these from /proc/self/environ once the plugin has read them, so the
        # secret does not linger readable to a co-tenant on the dropped uid.
        if env:
            child_env["ZU_OOP_SECRET_KEYS"] = ",".join(sorted(env))
        child_env["ZU_OOP_SOCK"] = sock
        child_env["ZU_OOP_TARGET"] = target_ref
        child_env["ZU_OOP_KIND"] = kind
        child_env["ZU_OOP_ARGS"] = args_json
        if user is not None:
            child_env["ZU_OOP_UID"] = str(user)
        proc = subprocess.Popen(
            [sys.executable, "-m", "zu_backends.oop_worker"], env=child_env
        )
        self._procs.append(proc)
        await self._await_socket(sock, proc)
        client = RpcClient(sock)
        self._clients.append(client)
        return client

    async def _await_socket(self, sock: str, proc: subprocess.Popen, timeout: float = 10.0) -> None:
        waited = 0.0
        while not os.path.exists(sock):
            if proc.poll() is not None:
                raise RuntimeError(f"oop worker exited early (code {proc.returncode}) before binding")
            if waited >= timeout:
                raise TimeoutError(f"oop worker did not bind {sock} within {timeout}s")
            await asyncio.sleep(0.02)
            waited += 0.02

    async def launch_channel(
        self,
        target_ref: str,
        endpoint: str,
        *,
        env: dict[str, str] | None = None,
        args_json: str = "{}",
        user: int | None = None,
    ) -> RemoteChannel:
        client = await self._spawn(target_ref, "channel", env=env, args_json=args_json, user=user)
        return RemoteChannel(client, endpoint)

    async def launch_tool(
        self,
        target_ref: str,
        *,
        env: dict[str, str] | None = None,
        args_json: str = "{}",
        user: int | None = None,
    ) -> RemoteTool:
        client = await self._spawn(target_ref, "tool", env=env, args_json=args_json, user=user)
        spec = await client.call("spec", {})
        return RemoteTool(client, spec)

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
        for proc in self._procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - last-resort kill
                proc.kill()
        self._clients.clear()
        self._procs.clear()
