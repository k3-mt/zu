"""The `zu mcp` server — drive Zu from any MCP coding agent.

A developer lives in their harness of choice (Claude Code, Cursor, …), types in
natural language, and the agent uses these tools to *design, validate, run, and
inspect* a Zu agent on their behalf — then streams the run back so they can watch
it work. It is a thin wrapper over the same engine the CLI uses (config, the
loop, the event bus), exposed over MCP's stdio transport.

The live stream-back is the point: ``zu_run`` subscribes to the event bus and
pushes every step — the model's train of thought, each tool call and result,
detector verdicts, escalations — to the client as an MCP log message *as it
happens*, using the same formatter as the CLI and the SSE stream. One formatter,
three surfaces.

Optional dependency (the ``mcp`` extra): ``pip install 'zu-runtime[mcp]'``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from zu_core.loop import run_task
from zu_core.registry import GROUPS, Registry

from .config import ConfigError, RunConfig, assemble, coerce_config, coerce_task
from .trace import format_event

log = logging.getLogger("zu.mcp")

# Config/task coercion (a tool arg may be a dict or a file path) is shared with
# `zu serve` and the embed facade — see zu_cli.config. The MCP tools accept a str
# task as a *path* (``allow_paths=True``): the agent driving these tools runs on
# the same host, so reading a task file it points at is intended.


def _discovered() -> dict[str, list[str]]:
    reg = Registry()
    reg.discover()
    return {kind: reg.names(kind) for kind in GROUPS}


def build_server() -> FastMCP:
    """Build the FastMCP server. Factored out so tests can drive the tools
    in-process via ``server.call_tool(...)``."""
    mcp = FastMCP("zu-runtime")

    @mcp.tool()
    async def zu_plugins() -> dict:
        """List every plugin Zu can discover here (providers, tools, detectors,
        validators, sinks, backends), so the agent knows what it can wire."""
        return _discovered()

    @mcp.tool()
    async def zu_scaffold(directory: str = ".", template: str = "web", force: bool = False) -> dict:
        """Create a starter agent.yaml in ``directory``. Templates:
        'web' (tier-1/2 web extraction), 'minimal' (no tools), 'research'
        (multi-field article extraction)."""
        from .scaffold import TEMPLATE_NAMES, write_template

        if template not in TEMPLATE_NAMES:
            return {"ok": False, "error": f"unknown template {template!r}; choose: {list(TEMPLATE_NAMES)}"}
        try:
            written = write_template(directory, template, force=force)
        except FileExistsError as exc:
            return {"ok": False, "error": f"files exist: {exc} (pass force=true to overwrite)"}
        return {
            "ok": True,
            "template": template,
            "files": written,
            "next": "Set the provider's API key, then call zu_validate, then zu_run.",
        }

    @mcp.tool()
    async def zu_validate(config: Any = None, task: Any = None) -> dict:
        """Validate a run config (and optionally a task) without executing: load
        it, discover and select plugins, and build the provider — surfacing any
        error with a clear message. ``config``/``task`` may be a path or a dict."""
        try:
            cfg = coerce_config(config)
            provider, registry, _bus, _providers = assemble(cfg)
            active = {kind: registry.names(kind) for kind in ("tools", "detectors", "validators")}
            checked_task = None
            if task is not None:
                spec = coerce_task(task, cfg.budget, allow_paths=True)
                checked_task = {"query": spec.query, "target": spec.target, "max_tier": spec.max_tier}
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "provider": cfg.provider.name,
            "model": getattr(provider, "model", None),
            "active_plugins": active,
            "task": checked_task,
        }

    @mcp.tool()
    async def zu_run(task: Any, config: Any = None, ctx: Context | None = None) -> dict:
        """Run a task and STREAM the run back live — every step (train of thought,
        tool calls, detectors, escalations) is sent to you as it happens — then
        return a concise result + run_id. ``task``/``config`` may be a path or a
        dict. Use zu_traces with the run_id to read the full event log."""
        try:
            cfg = coerce_config(config)
            spec = coerce_task(task, cfg.budget, allow_paths=True)
            provider, registry, bus, providers = assemble(cfg)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}

        async def _on_event(event: Any) -> None:
            line = format_event(event)
            if line and ctx is not None:
                try:
                    await ctx.info(line)  # live to the client; never break the run
                except Exception as exc:  # noqa: BLE001 - a transport hiccup must not break the run
                    log.debug("ctx.info failed (dropping a live trace line): %s", exc)

        bus.subscribe(_on_event)
        # The same observability hook: queue any blocked attempt for review.
        from .observe import attach_observability

        attach_observability(bus, cfg.observability)
        try:
            result = await run_task(spec, provider, registry, bus, providers=providers)
        except Exception as exc:  # noqa: BLE001 - a model/infra failure is data, not a crash
            return {"ok": False, "run_id": str(spec.task_id), "error": f"{type(exc).__name__}: {exc}"}

        return {
            "ok": result.status.value == "success",
            "run_id": str(spec.task_id),
            "status": result.status.value,
            "value": result.value,
            "reason": result.reason,
            "events": await bus.count(),
            "hint": "call zu_traces with this run_id to read the full event log",
        }

    @mcp.tool()
    async def zu_traces(
        db_path: str = "zu.db", run_id: str | None = None, limit: int = 100, after_seq: int = 0
    ) -> dict:
        """Read the event log (the always-on store) for a run — what the agent
        actually did. Filter by run_id; page forward with after_seq. Reads a
        sqlite trace sink (the default event_sink in the scaffolded config)."""
        try:
            from zu_backends.sqlite_sink import SqliteSink
        except ModuleNotFoundError:
            return {"ok": False, "error": "reading sqlite traces needs zu-backends (in zu-runtime base)"}
        flt = {"trace_id": run_id} if run_id else None
        sink = SqliteSink(db_path)
        events = await sink.query(flt, limit=limit, after_seq=after_seq)
        total = await sink.count(flt)
        return {
            "ok": True,
            "total": total,
            "returned": len(events),
            "events": [
                {"type": e.type, "source": e.source, "ts": e.ts.isoformat(), "payload": e.payload}
                for e in events
            ],
        }

    @mcp.resource("zu://plugins")
    def plugins_resource() -> str:
        """Everything Zu can discover here — context for designing a config."""
        return json.dumps(_discovered(), indent=2)

    @mcp.resource("zu://config/schema")
    def config_schema_resource() -> str:
        """The JSON schema of a Zu run config — so the agent writes valid YAML."""
        return json.dumps(RunConfig.model_json_schema(), indent=2)

    return mcp
