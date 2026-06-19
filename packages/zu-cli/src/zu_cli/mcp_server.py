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
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from zu_core.contracts import Budget, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import GROUPS, Registry

from .config import ConfigError, RunConfig, assemble, load_config, load_task
from .trace import format_event

# --- task/config coercion (a tool arg may be a dict or a file path) ----------


def _coerce_config(source: Any) -> RunConfig:
    if source is None:
        return load_config("zu.yaml")
    if isinstance(source, RunConfig):
        return source
    if isinstance(source, str):
        return load_config(source)
    if isinstance(source, dict):
        return RunConfig.model_validate(source)
    raise ConfigError(f"unsupported config type: {type(source).__name__}")


def _coerce_task(source: Any, default_budget: Budget) -> TaskSpec:
    if isinstance(source, TaskSpec):
        return source
    if isinstance(source, str):
        return load_task(source, default_budget=default_budget)
    if isinstance(source, dict):
        doc = dict(source)
        if "budget" not in doc:
            doc["budget"] = default_budget.model_dump()
        try:
            return TaskSpec.model_validate(doc)
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"invalid task: {exc}") from exc
    raise ConfigError(f"unsupported task type: {type(source).__name__}")


# --- scaffold templates ------------------------------------------------------

_TEMPLATES: dict[str, dict[str, str]] = {
    "web": {
        "zu.yaml": (
            "provider:\n"
            "  name: anthropic\n"
            "  model: claude-sonnet-4-6\n"
            "  api_key_env: ANTHROPIC_API_KEY\n"
            "plugins:\n"
            "  tools: [http_fetch, html_parse, render_dom]\n"
            "  detectors: [empty, error, js-shell, bot-wall]\n"
            "  validators: [schema, grounding]\n"
            "event_sink: { driver: sqlite, path: ./zu.db }\n"
            "budget: { max_steps: 20, max_tokens: 200000, wall_time_s: 120 }\n"
        ),
        "task.yaml": (
            "query: \"Extract the product name and price.\"\n"
            "target: \"https://example.com/product/123\"\n"
            "output_schema:\n"
            "  type: object\n"
            "  properties:\n"
            "    name: { type: string }\n"
            "    price: { type: string }\n"
            "  required: [name, price]\n"
        ),
    },
    "minimal": {
        "zu.yaml": (
            "provider:\n"
            "  name: anthropic\n"
            "  model: claude-sonnet-4-6\n"
            "  api_key_env: ANTHROPIC_API_KEY\n"
            "plugins:\n"
            "  validators: [schema]\n"
            "event_sink: { driver: sqlite, path: ./zu.db }\n"
        ),
        "task.yaml": (
            "query: \"Answer the question as JSON.\"\n"
            "output_schema:\n"
            "  type: object\n"
            "  properties: { answer: { type: string } }\n"
            "  required: [answer]\n"
        ),
    },
}


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
    async def zu_scaffold(directory: str = ".", template: str = "web") -> dict:
        """Create a starter zu.yaml + task.yaml in ``directory``. Templates:
        'web' (a tier-1/2 web-extraction agent) or 'minimal' (no tools)."""
        import os

        if template not in _TEMPLATES:
            return {"ok": False, "error": f"unknown template {template!r}; choose: {list(_TEMPLATES)}"}
        os.makedirs(directory, exist_ok=True)
        written = []
        for name, content in _TEMPLATES[template].items():
            path = os.path.join(directory, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(path)
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
            cfg = _coerce_config(config)
            provider, registry, _bus = assemble(cfg)
            active = {kind: registry.names(kind) for kind in ("tools", "detectors", "validators")}
            checked_task = None
            if task is not None:
                spec = _coerce_task(task, cfg.budget)
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
            cfg = _coerce_config(config)
            spec = _coerce_task(task, cfg.budget)
            provider, registry, bus = assemble(cfg)
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}

        async def _on_event(event: Any) -> None:
            line = format_event(event)
            if line and ctx is not None:
                try:
                    await ctx.info(line)  # live to the client; never break the run
                except Exception:  # noqa: BLE001
                    pass

        bus.subscribe(_on_event)
        try:
            result = await run_task(spec, provider, registry, bus)
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
