"""Zu — the embed facade. ``import zu`` and run an agent in one line.

This is the batteries-included entry point for *using* Zu from your own code.
It wires the same path the CLI and the HTTP server use — config in, a typed
``Result`` out — so embedding, ``zu run``, and ``zu serve`` are one runtime, not
three.

    import zu

    # one-shot, from files
    result = zu.run("task.yaml", config="zu.yaml")

    # or from plain dicts — no files needed
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

from zu_core.bus import EventBus
from zu_core.contracts import Budget, Event, Result, Status, TaskSpec
from zu_core.loop import run_task
from zu_cli.config import (
    ConfigError,
    RunConfig,
    assemble,
    load_config,
    load_task,
)

__version__ = "0.1.0"

__all__ = [
    "Zu",
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
    "__version__",
]


def _coerce_config(source: Any) -> RunConfig:
    """A RunConfig from a path (str), a dict, an existing RunConfig, or None
    (meaning ``./zu.yaml`` in the working directory)."""
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
    """A TaskSpec from a path (str), a dict, or an existing TaskSpec. A task that
    omits a budget inherits the config default."""
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
        except Exception as exc:  # noqa: BLE001 - surface as a ConfigError, not a raw pydantic error
            raise ConfigError(f"invalid task: {exc}") from exc
    raise ConfigError(f"unsupported task type: {type(source).__name__}")


class Zu:
    """A configured runner. Load a config once, run many tasks against it.

    ``config`` is a path, a dict, a ``RunConfig``, or None (``./zu.yaml``). The
    config is parsed eagerly so a bad config fails here, not on the first run.
    """

    def __init__(self, config: Any = None) -> None:
        self.config: RunConfig = _coerce_config(config)

    async def arun_with_events(self, task: Any) -> tuple[Result, list[Event]]:
        """Async: run one task, returning the Result and the run's event log."""
        spec = _coerce_task(task, self.config.budget)
        provider, registry, bus = assemble(self.config)
        result = await run_task(spec, provider, registry, bus)
        events = await bus.query()
        return result, events

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


def run(task: Any, config: Any = None) -> Result:
    """Run one task to a Result. ``task`` and ``config`` may each be a path, a
    dict, the typed object, or None (config → ``./zu.yaml``)."""
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
