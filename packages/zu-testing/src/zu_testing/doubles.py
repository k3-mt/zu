"""Shared test doubles — daemon-free, network-free stand-ins for the ports.

One canonical implementation of each, so the 44 test files across the workspace
stop reinventing subtly-different copies. Every double here implements (a subset
of) a real Zu port and records its interactions for assertions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx


def mock_transport(
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    *,
    text: str | None = None,
    status: int = 200,
) -> httpx.MockTransport:
    """An ``httpx.MockTransport`` for offline HTTP. Pass a ``handler`` for full
    control, or just ``text``/``status`` for the common "always return this body"
    case — replacing the per-file handler boilerplate in 7+ test modules."""
    if handler is None:
        body = text if text is not None else ""
        def handler(_request: httpx.Request) -> httpx.Response:  # noqa: E731 - tiny closure
            return httpx.Response(status, text=body)
    return httpx.MockTransport(handler)


class FakeSink:
    """A minimal in-memory ``EventSink`` with a synchronous ``appended`` view, for
    asserting append-before-notify ordering and what reached a destination."""

    name = "fake-sink"

    def __init__(self) -> None:
        self.appended: list[Any] = []
        self.closed = 0

    async def append(self, event: Any) -> None:
        self.appended.append(event)

    async def query(self, flt: dict | None = None, *, limit: int | None = None,
                    after_seq: int = 0) -> list[Any]:
        return list(self.appended)

    async def stream(self, flt: dict | None = None, *, batch_size: int = 500) -> AsyncIterator[Any]:
        for event in list(self.appended):
            yield event

    async def count(self, flt: dict | None = None) -> int:
        return len(self.appended)

    def close(self) -> None:
        self.closed += 1


class ExplodingSink:
    """An ``EventSink`` whose ``append`` always raises — to prove a sink failure
    propagates (the canonical store must never silently drop a record)."""

    name = "exploding-sink"

    async def append(self, event: Any) -> None:
        raise RuntimeError("disk is on fire")

    async def query(self, flt: dict | None = None, *, limit: int | None = None,
                    after_seq: int = 0) -> list[Any]:
        return []

    async def stream(self, flt: dict | None = None, *, batch_size: int = 500) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover - makes this an async generator

    async def count(self, flt: dict | None = None) -> int:
        return 0


class FakeSandboxBackend:
    """A daemon-free ``SandboxBackend`` covering BOTH ways tests use one:

    * ``exec(sandbox, call)`` — returns a saved rendered page (the tier-2 browser
      stand-in, for render_dom and the loop's escalation tests);
    * ``exec_entrypoint(sandbox, argv, environment=...)`` — returns saved stdout
      (the container-form runner stand-in), or delegates to ``on_exec_entrypoint``
      for tests that run the real in-container entrypoint in a thread.

    Records the lifecycle: ``launched`` (every spec), ``last_launch`` (the most
    recent), ``destroyed`` (count), and ``exec_env`` (the last entrypoint env)."""

    name = "fake-sandbox"

    def __init__(
        self,
        *,
        rendered: str = "",
        exec_output: str = "",
        exit_code: int = 0,
        exec_raises: bool = False,
        on_exec_entrypoint: Callable[[dict | None], tuple[int, str, str]] | None = None,
    ) -> None:
        self.rendered = rendered
        self.exec_output = exec_output
        self.exit_code = exit_code
        self.exec_raises = exec_raises
        self.on_exec_entrypoint = on_exec_entrypoint
        self.launched: list[dict] = []
        self.destroyed = 0
        self.exec_env: dict | None = None

    @property
    def last_launch(self) -> dict | None:
        return self.launched[-1] if self.launched else None

    async def launch(self, spec: dict) -> dict:
        self.launched.append(spec)
        return {"id": f"sbx-{len(self.launched)}", "spec": spec}

    async def exec(self, sandbox: Any, call: Any) -> dict:
        if self.exec_raises:
            raise RuntimeError("render blew up")
        return {"status": 200, "html": self.rendered, "url": call.args["url"]}

    async def exec_entrypoint(self, sandbox: Any, argv: list[str], *,
                              environment: dict | None = None,
                              timeout_s: float | None = None) -> tuple[int, str, str]:
        self.exec_env = environment
        if self.on_exec_entrypoint is not None:
            return self.on_exec_entrypoint(environment)
        return self.exit_code, self.exec_output, ""

    async def destroy(self, sandbox: Any) -> None:
        self.destroyed += 1
