"""``zu.Pipeline`` — the config-driven wrapper over the core orchestration.

The orchestration itself (shared-trace chaining, gating, resume) lives in
``zu_core.pipeline`` — pure, SDK-free core logic over ``run_task``. This wrapper
adds the ergonomics that match ``zu.run``: a YAML/dict **config** (one provider
block, the plugins, the sink) and **dict phase specs**. It assembles the config
once and shares the resulting bus across every phase.

    pipe = zu.Pipeline(config="zu.yaml")
    pipe.phase("extract",   {"query": "...", "output_schema": {...}})
    pipe.phase("summarize", lambda prev: {"query": f"...{prev.value['name']}...", "output_schema": {...}})
    result = pipe.run()          # PipelineResult: status, value, phases, events, id

A phase's task is a dict, or a callable ``(prev_result) -> dict`` that consumes
the previous phase's validated value. Point the config at the ``scripted``
provider to run the whole pipeline offline (no model, no network).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from zu_cli.config import assemble, coerce_config, coerce_task
from zu_core.contracts import Result, TaskSpec
from zu_core.pipeline import Phase, PipelineResult, run_pipeline

__all__ = ["Pipeline", "PipelineResult"]

# A phase's task: a static spec dict, or a builder that consumes the prior result.
PhaseTask = dict | Callable[[Result | None], dict]


class Pipeline:
    """A deterministic sequence of phases sharing one trace, log, and budget gate.

    ``config`` is a path, dict, ``RunConfig``, or None (``./zu.yaml``) — the same
    as ``zu.run``. ``pipeline_id`` defaults to a fresh id; pass a stable one (with
    a durable ``event_sink`` in the config) to make the pipeline resumable across
    process restarts.
    """

    def __init__(self, config: Any = None, *, pipeline_id: UUID | str | None = None) -> None:
        self.config = coerce_config(config)
        self._id = _as_uuid(pipeline_id) if pipeline_id is not None else uuid4()
        self._phases: list[tuple[str, PhaseTask]] = []

    @property
    def id(self) -> UUID:
        return self._id

    def phase(self, name: str, task: PhaseTask) -> Pipeline:
        """Append a phase. ``task`` is a spec dict or ``(prev_result) -> dict``.
        Returns self, so calls chain. Names must be unique (they key resume)."""
        if any(n == name for n, _ in self._phases):
            raise ValueError(f"duplicate phase name: {name!r}")
        self._phases.append((name, task))
        return self

    async def arun(self) -> PipelineResult:
        cfg = self.config
        provider, registry, bus, providers = assemble(cfg)
        try:
            phases = [Phase(name, self._build(task)) for name, task in self._phases]
            return await run_pipeline(
                phases, provider, registry, bus,
                providers=providers, containment=cfg.containment, pipeline_id=self._id,
                max_observation_chars=cfg.max_observation_chars,
            )
        finally:
            # ``assemble`` built the bus + its sink(s); release them after the run.
            await bus.aclose()

    def run(self) -> PipelineResult:
        """Run the pipeline synchronously."""
        return asyncio.run(self.arun())

    def _build(self, task: PhaseTask) -> Callable[[Result | None], TaskSpec]:
        """Turn a dict / callable phase task into a core TaskSpec builder, coercing
        the dict and inheriting the config's default budget."""
        def build(prev: Result | None) -> TaskSpec:
            task_dict = task(prev) if callable(task) else dict(task)
            return coerce_task(task_dict, self.config.budget, allow_paths=True)
        return build


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))
