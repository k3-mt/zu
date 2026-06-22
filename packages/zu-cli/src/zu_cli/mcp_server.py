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

The exploration tools (``zu_explore`` / ``zu_explore_save``) let the DEVELOPER's own
harness model pathfind a live site step by step and capture that discovery as the
agent's ``fixtures/`` bundle — so the frontier reasoning is spent once, in the harness
they already use, and the path replays free thereafter.

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

    # One live pathfinding session per server process (a developer explores one site at a
    # time in their harness). Held in a mutable cell the explore tools share.
    _explore: dict[str, Any] = {"session": None}

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

    @mcp.tool()
    async def zu_explore(
        tool: str, op: str | None = None, url: str | None = None,
        actions: list | None = None, capture_network: bool = False,
        wait_until: str | None = None, html: bool = False,
    ) -> dict:
        """Pathfind a LIVE site one step at a time — YOU (the harness model) drive zu's
        off-box tool, see the observation, and decide the next step; the trail is recorded.
        The session persists across calls. When the path reaches the data you need, call
        ``zu_explore_save`` to capture it as the agent's fixtures.

        ``tool`` is one of: ``http_fetch`` / ``render_dom`` (one-shot — pass ``url``) or
        ``browser`` (a PERSISTENT session — pass ``op`` open/act/read/close, plus ``url`` for
        open and ``actions`` for act). Tip: fetch the page first; if it's a JS shell, drive
        the browser — that fetch step is what lets the agent escalate offline later."""
        from .explore import EXPLORABLE, new_session

        if tool not in EXPLORABLE:
            return {"ok": False, "error": f"unknown tool {tool!r}; choose one of {list(EXPLORABLE)}"}
        args: dict[str, Any] = {}
        if tool in ("http_fetch", "render_dom"):
            if not url:
                return {"ok": False, "error": f"{tool} needs a url"}
            args["url"] = url
            if tool == "render_dom" and wait_until:
                args["wait_until"] = wait_until
        else:  # browser
            if not op:
                return {"ok": False, "error": "browser needs an op (open/act/read/close)"}
            args["op"] = op
            if url:
                args["url"] = url
            if actions:
                args["actions"] = actions
            if capture_network:
                args["capture_network"] = True
        if html:
            args["html"] = True
        if _explore["session"] is None:
            _explore["session"] = new_session()
        try:
            obs = await _explore["session"].step(tool, args)
        except Exception as exc:  # noqa: BLE001 - a tool/SSRF failure is data for the harness
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "step": len(_explore["session"].steps), "observation": obs}

    @mcp.tool()
    async def zu_explore_save(agent: str, task: str, answer: Any) -> dict:
        """Capture the current exploration as the agent's ``fixtures/capture.json`` — the
        discovered path becomes a replayable bundle (then ``zu build`` hardens it into a
        track). ``task`` is the query the agent will run; ``answer`` is the final value you
        read (what the agent should produce). Ends the session."""
        from pathlib import Path

        from .offline import bundle_path

        sess = _explore["session"]
        if sess is None or not sess.steps:
            return {"ok": False, "error": "no exploration to save — call zu_explore first"}
        p = Path(agent)
        agent_dir = p if p.is_dir() else p.parent
        out = bundle_path(agent_dir)
        out.parent.mkdir(parents=True, exist_ok=True)
        sess.to_bundle(task=task, answer=answer).save(out)
        steps = len(sess.steps)
        tools_seen = sorted({s["tool"] for s in sess.steps})
        _explore["session"] = None  # a save finalizes the session
        return {
            "ok": True, "bundle": str(out), "steps": steps, "tools": tools_seen,
            "next": "`zu run --offline` replays it at ~$0; `zu build` hardens it into a track.",
        }

    @mcp.tool()
    async def zu_explore_reset() -> dict:
        """Discard the current exploration (close any open browser) and start fresh."""
        sess = _explore["session"]
        if sess is not None and "browser" in sess.tools:
            try:
                await sess.tools["browser"](sess.ctx, op="close")
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        _explore["session"] = None
        return {"ok": True}

    @mcp.tool()
    async def zu_report_gap(
        agent: str, summary: str, expected: str, observed: str, proposed: str | None = None,
    ) -> dict:
        """When zu genuinely CAN'T do something — a missing tool/primitive, a detector that
        won't fire, a selector zu can't resolve, a soft miss it mishandles — that's a
        CAPABILITY GAP in zu, not a bug in your agent. **Don't hardcode around it.** This
        builds a strong, REPEATABLE issue for the zu repo: it embeds the agent's `agent.yaml`,
        points at its `fixtures/` bundle (a $0 deterministic repro the maintainers reproduce
        with `zu run --offline`), and records expected vs observed + a proposed GENERIC
        capability. Writes `gap-report.md` next to the agent and returns a ready
        `gh issue create` command. Capture a bundle first (`zu_explore` / `zu capture`) so the
        gap reproduces — see the `zu://contributing` resource."""
        from pathlib import Path

        from .contribute import ZU_REPO, build_gap_report

        p = Path(agent)
        agent_dir = p if p.is_dir() else p.parent
        report = build_gap_report(
            agent_dir, summary=summary, expected=expected, observed=observed, proposed=proposed)
        out = agent_dir / "gap-report.md"
        out.write_text(report.body, encoding="utf-8")
        return {
            "ok": True,
            "title": report.title,
            "issue_markdown": report.body,
            "has_repro": report.has_repro,
            "repro": report.repro_path,
            "report_file": str(out),
            "repo": ZU_REPO,
            "gh_command": report.gh_command(str(out)),
            "next": (
                "A repeatable fixtures repro is attached — file it (gh_command); the "
                "maintainers' agent reproduces it with `zu run --offline` and builds the "
                "generic capability." if report.has_repro else
                "⚠️ No fixtures bundle — capture one first (zu_explore / zu capture) so the "
                "gap reproduces deterministically, then re-run zu_report_gap."
            ),
        }

    @mcp.resource("zu://contributing")
    def contributing_resource() -> str:
        """When and how to contribute a capability gap upstream — read this on any wall."""
        return (
            "Contributing to zu — the no-hardcoding contract.\n\n"
            "zu's rule: when you hit a wall you do NOT hardcode around it. The model reasons; "
            "tools expose generic primitives. So if zu can't do something you need, that is a "
            "CAPABILITY GAP in zu — file it upstream rather than working around it locally.\n\n"
            "What counts as a gap (not a user error): a missing tool/primitive; a detector "
            "that should have escalated but didn't; a selector or control zu can't resolve; a "
            "soft miss the loop mishandles; a tier ladder that can't express your flow.\n\n"
            "How to file a strong one: capture a repeatable example first — drive the path "
            "with `zu_explore` (your harness pathfinds the live site) or `zu capture` (one "
            "live run), which records `fixtures/capture.json`. That bundle reproduces the run "
            "deterministically at $0, so the maintainers reproduce the gap with "
            "`zu run --offline` and the maintainers' agent picks it up. Then call "
            "`zu_report_gap` to build the issue (agent.yaml + the repro + expected/observed + "
            "a proposed GENERIC capability) and a ready `gh issue create` command.\n\n"
            "The fix that lands will be a generic capability — which then helps every zu user, "
            "the same way the existing capabilities were built."
        )

    @mcp.resource("zu://plugins")
    def plugins_resource() -> str:
        """Everything Zu can discover here — context for designing a config."""
        return json.dumps(_discovered(), indent=2)

    @mcp.resource("zu://config/schema")
    def config_schema_resource() -> str:
        """The JSON schema of a Zu run config — so the agent writes valid YAML."""
        return json.dumps(RunConfig.model_json_schema(), indent=2)

    return mcp
