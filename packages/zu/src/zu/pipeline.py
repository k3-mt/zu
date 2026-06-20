"""Multi-phase pipelines — chaining runs without losing the per-run guarantees.

A single Zu run is robust because it is event-sourced: lossless, replayable,
validated, budgeted. A naive ``zu.run`` x N loses that *at the seams* — each
phase a separate trace, the orchestration unrecorded, no resume. ``Pipeline``
lifts those guarantees to the whole sequence:

* **one trace, one log** — every phase shares a pipeline ``trace_id`` and writes
  to the same canonical sink, so the entire multi-phase run is one replayable
  lineage (each phase still has its own ``task_id`` and its own validation).
* **gate on success** — a phase advances only when the previous one finished
  ``SUCCESS`` (the schema/grounding validators decide "satisfied"); on failure
  the pipeline stops with the log intact.
* **resume from the log** — a re-run with the same ``pipeline_id`` and a durable
  sink skips phases already completed on the log and reuses their values, so a
  crashed pipeline restarts where it stopped instead of repeating finished work.

    pipe = zu.Pipeline(config="zu.yaml")
    pipe.phase("extract",   {"query": "...", "output_schema": {...}})
    pipe.phase("summarize", lambda prev: {"query": f"...{prev.value['name']}...", "output_schema": {...}})
    result = pipe.run()          # PipelineResult: status, value, phases, events, id

A phase's task is a dict, or a callable ``(prev_result) -> dict`` that consumes
the previous phase's validated value. Offline-friendly: point the config at the
``scripted`` provider and the whole pipeline replays with no model, no network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4, uuid5

from zu_cli.config import assemble, coerce_config, coerce_task
from zu_core import events as ev
from zu_core.contracts import Event, Result, Status
from zu_core.loop import run_task

# A phase's task: a static spec dict, or a builder that consumes the prior result.
PhaseTask = dict | Callable[[Result | None], dict]


@dataclass
class _Phase:
    name: str
    task: PhaseTask


@dataclass
class PipelineResult:
    """The outcome of a multi-phase run."""

    id: UUID                                  # the pipeline trace id (query the log by this)
    status: Status                            # SUCCESS iff every phase succeeded
    value: dict | None                        # the final phase's value
    phases: dict[str, Result] = field(default_factory=dict)   # per-phase results
    events: list[Event] = field(default_factory=list)         # the whole pipeline log
    failed_phase: str | None = None


class Pipeline:
    """A deterministic sequence of phases sharing one trace, log, and budget gate.

    ``config`` is a path, dict, ``RunConfig``, or None (``./zu.yaml``) — the same
    as ``zu.run``. ``pipeline_id`` defaults to a fresh id; pass a stable one (with
    a durable ``event_sink`` in the config) to make a pipeline resumable across
    process restarts.
    """

    def __init__(self, config: Any = None, *, pipeline_id: UUID | str | None = None) -> None:
        self.config = coerce_config(config)
        self._id = _as_uuid(pipeline_id) if pipeline_id is not None else uuid4()
        self._phases: list[_Phase] = []

    @property
    def id(self) -> UUID:
        return self._id

    def phase(self, name: str, task: PhaseTask) -> Pipeline:
        """Append a phase. ``task`` is a spec dict or ``(prev_result) -> dict``.
        Returns self, so calls chain. Names must be unique (they key resume)."""
        if any(p.name == name for p in self._phases):
            raise ValueError(f"duplicate phase name: {name!r}")
        self._phases.append(_Phase(name, task))
        return self

    async def arun(self) -> PipelineResult:
        cfg = self.config
        provider, registry, bus, providers = assemble(cfg)
        trace = self._id
        phases: dict[str, Result] = {}
        prev: Result | None = None
        failed: str | None = None
        try:
            await _emit(bus, trace, ev.PIPELINE_STARTED,
                        {"phases": [p.name for p in self._phases]})
            for p in self._phases:
                task_id = uuid5(trace, p.name)   # deterministic per (pipeline, phase)
                done = await _completed_value(bus, trace, task_id)
                if done is not None:
                    # Resume: this phase is already on the log — reuse, don't re-run.
                    prev = Result(status=Status.SUCCESS, value=done)
                    phases[p.name] = prev
                    await _emit(bus, trace, ev.PIPELINE_PHASE_SKIPPED,
                                {"phase": p.name}, task_id=task_id)
                    continue

                task_dict = p.task(prev) if callable(p.task) else dict(p.task)
                spec = coerce_task(task_dict, cfg.budget, allow_paths=True)
                spec = spec.model_copy(update={"task_id": task_id})
                await _emit(bus, trace, ev.PIPELINE_PHASE_STARTED,
                            {"phase": p.name}, task_id=task_id)
                result = await run_task(
                    spec, provider, registry, bus,
                    providers=providers, containment=cfg.containment, trace_id=trace,
                )
                phases[p.name] = result
                await _emit(bus, trace, ev.PIPELINE_PHASE_COMPLETED,
                            {"phase": p.name, "status": result.status.value}, task_id=task_id)

                if result.status is not Status.SUCCESS:
                    failed = p.name
                    await _emit(bus, trace, ev.PIPELINE_FAILED,
                                {"phase": p.name, "reason": result.reason})
                    break
                prev = result

            if failed is None:
                await _emit(bus, trace, ev.PIPELINE_COMPLETED, {})
            events = await bus.query({"trace_id": trace})
        finally:
            await bus.aclose()

        status = Status.SUCCESS if failed is None else phases[failed].status
        value = prev.value if (failed is None and prev is not None) else None
        return PipelineResult(
            id=trace, status=status, value=value,
            phases=phases, events=events, failed_phase=failed,
        )

    def run(self) -> PipelineResult:
        """Run the pipeline synchronously."""
        return asyncio.run(self.arun())


def _as_uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


async def _emit(bus: Any, trace: UUID, type_: str, payload: dict,
                task_id: UUID | None = None) -> None:
    """Write a pipeline-boundary event to the shared log under the pipeline trace.
    Pipeline-level events carry ``task_id == trace``; phase events carry the
    phase's task id, so ``{trace_id: pipeline}`` returns the whole run."""
    await bus.publish(Event(
        trace_id=trace, task_id=task_id if task_id is not None else trace,
        type=type_, source="pipeline", payload=payload,
    ))


async def _completed_value(bus: Any, trace: UUID, task_id: UUID) -> dict | None:
    """The value a phase recorded if it already completed on the log, else None —
    the resume check. Reads the phase's own ``data.record.extracted`` event under
    the shared trace."""
    done = await bus.query({"trace_id": trace, "task_id": task_id,
                            "type": ev.TASK_COMPLETED})
    if not done:
        return None
    return done[-1].payload.get("value")
