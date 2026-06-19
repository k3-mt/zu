"""`zu serve` — a thin HTTP wrapper over the same run path as the CLI.

One endpoint that matters: ``POST /run`` takes a task (and an optional config
override), drives the interpreter loop, and returns the ``Result`` plus the
run's event log. It is a *wrapper*, not a second code path — it assembles the
provider/registry/bus from config exactly as ``zu run`` does, so behaviour is
identical whether you embed the library, run the CLI, or call the service.

FastAPI is an optional dependency (the ``serve`` extra): the import lives inside
``create_app`` so ``import zu_cli`` stays cheap and the core never requires a web
framework. Install it with ``pip install 'zu-cli[serve]'``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from zu_core.contracts import Budget
from zu_core.loop import run_task

from .config import ConfigError, RunConfig, assemble, load_config


class RunRequest(BaseModel):
    """The POST /run body. Defined at module scope (not inside create_app) so
    FastAPI can resolve the annotation under ``from __future__ import annotations``."""

    task: dict = Field(..., description="The task spec (query, target, output_schema, ...).")
    config: dict | None = Field(
        None, description="Optional per-request config override; omit to use the server default."
    )
    include_events: bool = Field(True, description="Return the run's event log alongside the result.")


def _coerce_config(source: Any) -> RunConfig:
    """A config from a path, a dict, an already-parsed RunConfig, or None
    (meaning ``./zu.yaml``)."""
    if source is None:
        return load_config("zu.yaml")
    if isinstance(source, RunConfig):
        return source
    if isinstance(source, str):
        return load_config(source)
    if isinstance(source, dict):
        return RunConfig.model_validate(source)
    raise ConfigError(f"unsupported config type: {type(source).__name__}")


def _coerce_task(source: Any, default_budget: Budget) -> Any:
    """A task from a dict or an already-built TaskSpec. The server takes the task
    in the request body (a path would be server-side, which a client can't set),
    so str paths are intentionally not accepted here."""
    from zu_core.contracts import TaskSpec

    if isinstance(source, TaskSpec):
        return source
    if isinstance(source, dict):
        doc = dict(source)
        doc.setdefault("budget", default_budget.model_dump())
        try:
            return TaskSpec.model_validate(doc)
        except Exception as exc:  # noqa: BLE001 - a 422 with a message, not a 500 crash
            raise ConfigError(f"invalid task: {exc}") from exc
    raise ConfigError("task must be a JSON object (the task spec)")


def create_app(config: Any = None, *, title: str = "Zu") -> Any:
    """Build the ASGI app. ``config`` is the server's default run config (path,
    dict, RunConfig, or None for ./zu.yaml); a request may override it per call.

    Raises a clear error at construction time if the default config can't be
    loaded — fail fast on startup, not on the first request."""
    try:
        from fastapi import FastAPI, HTTPException
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via message
        raise RuntimeError(
            "the HTTP server needs FastAPI; install it with: pip install 'zu-cli[serve]'"
        ) from exc

    default_cfg = _coerce_config(config)

    app = FastAPI(title=title, description="Zu — Agent Production Runtime")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/run")
    async def run_endpoint(req: RunRequest) -> dict:
        try:
            cfg = _coerce_config(req.config) if req.config is not None else default_cfg
            spec = _coerce_task(req.task, cfg.budget)
            provider, registry, bus = assemble(cfg)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        try:
            result = await run_task(spec, provider, registry, bus)
        except Exception as exc:  # noqa: BLE001 - a model/infra failure is a 502, not a crash
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}")

        body: dict = {"result": result.model_dump(mode="json")}
        if req.include_events:
            events = await bus.query()
            body["events"] = [e.model_dump(mode="json") for e in events]
        return body

    return app
