"""The `zu mcp` server — drive Zu from any MCP coding agent.

A developer lives in their harness of choice (Claude Code, Cursor, …), types in
natural language, and the agent uses these tools to *design, validate, run,
inspect* — and *construct* — a Zu agent on their behalf, then streams the run back
so they can watch it work. It is a thin wrapper over the same engine the CLI uses
(config, the loop, the event bus), exposed over MCP's stdio transport.

The construction tools (``zu_offline_run`` / ``zu_build`` / ``zu_harden`` /
``zu_construct``) expose the offline construction sequence — replay a captured
``fixtures/`` bundle, build a hardened track, score resilience, and run the
anti-hardcode readiness gate — all at ~$0 (no model, no network). They are the
surface the autonomous meta-agent drives: an external agent reads the readiness
violations, edits the agent, and re-checks until it clears the gate.

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


def _load_for_construction(agent: str) -> tuple[Any, Any, Any, Any]:
    """Load ``(spec, cfg, agent_dir, bundle)`` for a construction tool. Raises
    ``ConfigError`` (bad agent) or ``OfflineError`` (no ``fixtures/`` bundle yet) — the
    caller turns either into a clean ``{"ok": False, "error": ...}``."""
    from pathlib import Path

    from .config import load_agent
    from .offline import Bundle, bundle_path

    spec, cfg = load_agent(agent)
    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent
    return spec, cfg, agent_dir, Bundle.load(bundle_path(agent_dir))


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

    @mcp.tool()
    async def zu_offline_run(agent: str) -> dict:
        """Replay an agent against its captured ``fixtures/`` bundle — no model, no network,
        ~$0. The agent must have a ``fixtures/capture.json`` (from ``zu capture``). Returns
        the result and whether it succeeded — the cheap inner loop of construction."""
        from .offline import OfflineError, replay_offline

        try:
            spec, cfg, _dir, bundle = _load_for_construction(agent)
        except (ConfigError, OfflineError) as exc:
            return {"ok": False, "error": str(exc)}
        result, events = await replay_offline(spec, cfg, bundle)
        return {
            "ok": result.status.value == "success",
            "status": result.status.value,
            "value": result.value,
            "reason": result.reason,
            "events": len(events),
        }

    @mcp.tool()
    async def zu_build(agent: str, min_resilience: float = 1.0) -> dict:
        """Run the offline construction spine — build → record track → harden — at ~$0, and
        write a hardened ``track.json`` next to the agent. Returns each stage's outcome, the
        track path, and the resilience score. Needs a captured bundle."""
        from .build import build_offline
        from .offline import OfflineError

        try:
            spec, cfg, agent_dir, bundle = _load_for_construction(agent)
        except (ConfigError, OfflineError) as exc:
            return {"ok": False, "error": str(exc)}
        report = await build_offline(spec, cfg, agent_dir, bundle, min_score=min_resilience)
        return {
            "ok": report.ok,
            "stages": [{"name": s.name, "status": s.status, "detail": s.detail}
                       for s in report.stages],
            "track_path": report.track_path,
            "resilience": report.harden.resilience if report.harden else None,
        }

    @mcp.tool()
    async def zu_harden(agent: str) -> dict:
        """Score how brittle a captured path is — replay perturbed fixtures offline (~$0).
        Returns the resilience score (fraction of cosmetic page changes the path absorbs),
        whether grounding is load-bearing (the score is only meaningful if value-deletion
        controls fail), and the static brittleness findings to fix."""
        from .harden import harden
        from .offline import OfflineError

        try:
            spec, cfg, _dir, bundle = _load_for_construction(agent)
        except (ConfigError, OfflineError) as exc:
            return {"ok": False, "error": str(exc)}
        hr = await harden(spec, cfg, bundle)
        return {
            "ok": True,
            "resilience": hr.resilience,
            "grounding_load_bearing": hr.grounding_load_bearing,
            "findings": [{"kind": f.kind, "where": f.where, "detail": f.detail}
                         for f in hr.findings],
        }

    @mcp.tool()
    async def zu_construct(agent: str, min_resilience: float = 1.0) -> dict:
        """The construction-readiness gate (one round, no model, ~$0): the offline build
        plus the anti-hardcode guardrails (G1 alternate locators, G2 resilience, G3 no
        hardcoded answer). Returns whether the agent is ready for promotion and, if not, the
        violations to fix — the loop an autonomous agent drives: read the violations, edit
        the agent, call again until ``ready`` is true. Never promotes (review gate G4)."""
        from .build import build_offline
        from .guardrails import enforce_guardrails
        from .offline import OfflineError

        try:
            spec, cfg, agent_dir, bundle = _load_for_construction(agent)
        except (ConfigError, OfflineError) as exc:
            return {"ok": False, "error": str(exc)}
        build = await build_offline(spec, cfg, agent_dir, bundle, min_score=min_resilience)
        guards = await enforce_guardrails(
            spec, cfg, bundle, agent_dir, min_resilience=min_resilience)
        return {
            "ok": True,
            "ready": build.ok and guards.passed,
            "build_ok": build.ok,
            "guardrails_passed": guards.passed,
            "resilience": guards.resilience,
            "violations": [{"rule": v.rule, "detail": v.detail} for v in guards.violations],
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
