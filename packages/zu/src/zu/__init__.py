"""Zu — the embed facade. ``import zu`` and run an agent in one line.

This is the batteries-included entry point for *using* Zu from your own code.
It wires the same path the CLI and the HTTP server use — config in, a typed
``Result`` out — so embedding, ``zu run``, and ``zu serve`` are one runtime, not
three.

    import zu

    # a self-contained agent: one agent.yaml, or a bundle dir (agent.yaml + tools/)
    result = zu.run_agent("agent.yaml")        # or zu.run_agent("my-agent/")

    # the programmatic form — a config plus a task (config + many tasks):
    result = zu.run(
        {"query": "Extract the title and price.", "target": "https://example.com",
         "output_schema": {"type": "object", "properties": {"title": {"type": "string"}}}},
        config={"provider": {"name": "anthropic", "model": "claude-sonnet-4-6",
                             "api_key_env": "ANTHROPIC_API_KEY"},
                "plugins": {"tools": ["http_fetch", "html_parse"], "validators": ["schema"]}},
    )

    print(result.status, result.value)

    # a reusable, configured runner (load config once, run many tasks)
    agent = zu.Zu(config="zu.yaml")
    r1 = agent.run({"query": "..."})
    r2, events = agent.run_with_events({"query": "..."})   # also get the event log

Credentials are never passed here: config names the *environment variable*
holding a key (``api_key_env``), resolved inside the adapter at call time.
"""

from __future__ import annotations

import asyncio
from typing import Any

from zu_cli.config import (
    ConfigError,
    RunConfig,
    assemble,
    coerce_config,
    coerce_task,
)
from zu_core.contracts import Budget, Event, Result, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import (
    backend,
    detector,
    provider,
    sink,
    tool,
    validator,
)

from .pipeline import Pipeline, PipelineResult

__version__ = "0.1.0"

# In-process plugin registration decorators, re-exported from the core registry
# so the documented ``@zu.tool`` / ``@zu.detector`` / … surface (see
# the architecture docs and AGENTS.md) actually resolves on ``import zu``. They
# register onto the process-wide REGISTRY the loop reads, so a decorator-
# registered plugin is visible to ``zu.run`` and ``zu plugins`` alike.
__all__ = [
    "Zu",
    "Pipeline",
    "PipelineResult",
    "run_agent",
    "run_agent_with_events",
    "run",
    "arun",
    "run_with_events",
    "ConfigError",
    "RunConfig",
    "TaskSpec",
    "Result",
    "Status",
    "Budget",
    "Event",
    "create_app",
    "tool",
    "detector",
    "validator",
    "provider",
    "backend",
    "sink",
    "__version__",
]


# Config/task coercion is shared with the CLI surfaces (see zu_cli.config). The
# embed facade accepts a str task as a *path* (``allow_paths=True``): you're
# running in-process on your own host, so reading a task file you point at — the
# same affordance as ``zu run`` — is intended.


class Zu:
    """A configured runner. Load a config once, run many tasks against it.

    ``config`` is a path, a dict, a ``RunConfig``, or None (``./zu.yaml``). The
    config is parsed eagerly so a bad config fails here, not on the first run.
    """

    def __init__(self, config: Any = None) -> None:
        self.config: RunConfig = coerce_config(config)

    async def arun_with_events(self, task: Any) -> tuple[Result, list[Event]]:
        """Async: run one task, returning the Result and the run's event log."""
        spec = coerce_task(task, self.config.budget, allow_paths=True)
        provider, registry, bus, providers = assemble(self.config)
        # The same observability hook the CLI uses: an embedded agent queues a
        # blocked attempt to the review queue too (no console trace by default).
        from zu_cli.observe import attach_observability

        attach_observability(bus, self.config.observability)
        try:
            result = await run_task(
                spec, provider, registry, bus,
                providers=providers, containment=self.config.containment,
                max_observation_chars=self.config.max_observation_chars,
                observation_strategy=self.config.observation_strategy,
            )
            events = await bus.query()
            return result, events
        finally:
            # ``assemble`` builds a fresh bus (and its canonical/trace sinks) per
            # run; release them here so a long-lived, reused ``Zu`` instance does
            # not leak one sqlite connection per ``run()``.
            await bus.aclose()

    async def arun(self, task: Any) -> Result:
        """Async: run one task, returning just the Result."""
        result, _ = await self.arun_with_events(task)
        return result

    def run_with_events(self, task: Any) -> tuple[Result, list[Event]]:
        """Run one task synchronously, returning the Result and the event log."""
        return asyncio.run(self.arun_with_events(task))

    def run(self, task: Any) -> Result:
        """Run one task synchronously, returning just the Result."""
        return asyncio.run(self.arun(task))


def run_agent(source: Any = None) -> Result:
    """Run a self-contained agent to a Result — the embed equivalent of
    ``zu run``. ``source`` is an ``agent.yaml`` path, a **bundle directory**
    (agent.yaml + a tools/ package, auto-loaded), a dict, or None (``./agent.yaml``
    or ``./``)."""
    result, _ = run_agent_with_events(source)
    return result


def run_agent_with_events(source: Any = None) -> tuple[Result, list[Event]]:
    """Run a self-contained agent, returning the Result *and* its event log."""
    return asyncio.run(_arun_agent(source))


async def _arun_agent(source: Any) -> tuple[Result, list[Event]]:
    from zu_cli.config import load_agent
    from zu_cli.observe import attach_observability

    spec, cfg = load_agent(source)
    provider, registry, bus, providers = assemble(cfg)
    attach_observability(bus, cfg.observability)
    try:
        result = await run_task(
            spec, provider, registry, bus,
            providers=providers, containment=cfg.containment,
            max_observation_chars=cfg.max_observation_chars,
            observation_strategy=cfg.observation_strategy,
        )
        return result, await bus.query()
    finally:
        await bus.aclose()


def run(task: Any, config: Any = None) -> Result:
    """Run one task against a config — the programmatic form (config + many tasks).
    ``task`` and ``config`` may each be a path, a dict, the typed object, or None.
    For a single self-contained ``agent.yaml``/bundle, use :func:`run_agent`."""
    return Zu(config).run(task)


async def arun(task: Any, config: Any = None) -> Result:
    """Async one-shot — the coroutine behind :func:`run`."""
    return await Zu(config).arun(task)


def run_with_events(task: Any, config: Any = None) -> tuple[Result, list[Event]]:
    """Run one task to a Result *and* its event log (the queryable provenance)."""
    return Zu(config).run_with_events(task)


def create_app(config: Any = None, **kwargs: Any) -> Any:
    """The ASGI app for ``zu serve``. Re-exported here so an embedder can mount
    Zu in their own ASGI stack. Needs the 'serve' extra (FastAPI)."""
    from zu_cli.server import create_app as _create_app

    return _create_app(config, **kwargs)
