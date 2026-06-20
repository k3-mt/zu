"""Multi-phase orchestration — chaining runs under one event-sourced lineage.

A single run (``run_task``) is lossless, replayable, validated, budgeted. A
multi-phase agent is a *sequence* of runs, and the robust way to chain them keeps
those guarantees across the seams: every phase shares one ``trace_id`` and one
event log, a phase advances only on the previous one's validated success, and a
re-run **resumes from the log** instead of repeating finished work.

This is core orchestration over ``run_task`` and the bus — no model SDK, no
config system — so it lives here beside the loop. The config-driven ergonomics
(``zu.Pipeline``: a YAML/dict config, dict phase specs) are a thin wrapper in the
embed facade, because config/assembly is a ``zu-cli`` concern the core must not
depend on. A phase here is a builder that returns a ready ``TaskSpec``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4, uuid5

from . import events as ev
from .bus import EventBus
from .contracts import Event, Result, Status, TaskSpec
from .loop import run_task
from .ports import ModelProvider
from .registry import REGISTRY, Registry

# A phase builds its TaskSpec, optionally from the previous phase's Result.
PhaseBuild = Callable[[Result | None], TaskSpec]


@dataclass
class Phase:
    """One phase: a name (stable — it keys resume) and a TaskSpec builder."""

    name: str
    build: PhaseBuild


@dataclass
class PipelineResult:
    """The outcome of a multi-phase run."""

    id: UUID                                   # the pipeline trace id (query the log by this)
    status: Status                             # SUCCESS iff every phase succeeded
    value: dict | None                         # the final phase's value
    phases: dict[str, Result] = field(default_factory=dict)   # per-phase results
    events: list[Event] = field(default_factory=list)         # the whole pipeline log
    failed_phase: str | None = None


async def run_pipeline(
    phases: Sequence[Phase],
    provider: ModelProvider,
    registry: Registry | None = None,
    bus: EventBus | None = None,
    *,
    providers: Mapping[int, ModelProvider] | None = None,
    containment: str = "audit",
    pipeline_id: UUID | None = None,
    max_observation_chars: int | None = None,
    observation_strategy: str = "truncate",
) -> PipelineResult:
    """Drive ``phases`` to a ``PipelineResult`` under one shared trace.

    All phases write to the one ``bus`` (the caller owns its lifecycle — this does
    not close it), share ``pipeline_id`` as their ``trace_id`` (each keeps its own
    ``task_id``), and advance only on a ``SUCCESS``. A phase already completed on
    the log (same trace + phase task id) is skipped and its value reused — pass a
    durable sink + a stable ``pipeline_id`` to resume across process restarts.
    """
    registry = registry if registry is not None else REGISTRY
    bus = bus or EventBus()
    trace = pipeline_id if pipeline_id is not None else uuid4()
    results: dict[str, Result] = {}
    prev: Result | None = None
    failed: str | None = None

    await _emit(bus, trace, ev.PIPELINE_STARTED, {"phases": [p.name for p in phases]})
    for phase in phases:
        task_id = uuid5(trace, phase.name)   # deterministic per (pipeline, phase)
        done = await _completed_value(bus, trace, task_id)
        if done is not None:
            # Resume: already on the log — reuse the value, don't re-run.
            prev = Result(status=Status.SUCCESS, value=done)
            results[phase.name] = prev
            await _emit(bus, trace, ev.PIPELINE_PHASE_SKIPPED, {"phase": phase.name}, task_id=task_id)
            continue

        spec = phase.build(prev).model_copy(update={"task_id": task_id})
        await _emit(bus, trace, ev.PIPELINE_PHASE_STARTED, {"phase": phase.name}, task_id=task_id)
        result = await run_task(
            spec, provider, registry, bus,
            providers=providers, containment=containment, trace_id=trace,
            max_observation_chars=max_observation_chars,
            observation_strategy=observation_strategy,
        )
        results[phase.name] = result
        await _emit(bus, trace, ev.PIPELINE_PHASE_COMPLETED,
                    {"phase": phase.name, "status": result.status.value}, task_id=task_id)
        if result.status is not Status.SUCCESS:
            failed = phase.name
            await _emit(bus, trace, ev.PIPELINE_FAILED,
                        {"phase": phase.name, "reason": result.reason})
            break
        prev = result

    if failed is None:
        await _emit(bus, trace, ev.PIPELINE_COMPLETED, {})

    events = await bus.query({"trace_id": trace})
    status = Status.SUCCESS if failed is None else results[failed].status
    value = prev.value if (failed is None and prev is not None) else None
    return PipelineResult(
        id=trace, status=status, value=value,
        phases=results, events=events, failed_phase=failed,
    )


async def _emit(bus: EventBus, trace: UUID, type_: str, payload: dict,
                task_id: UUID | None = None) -> None:
    """A pipeline-boundary event under the pipeline trace. Pipeline-level events
    carry ``task_id == trace``; phase events carry the phase's task id, so
    ``{trace_id: pipeline}`` returns the whole run."""
    await bus.publish(Event(
        trace_id=trace, task_id=task_id if task_id is not None else trace,
        type=type_, source="pipeline", payload=payload,
    ))


async def _completed_value(bus: EventBus, trace: UUID, task_id: UUID) -> dict | None:
    """The value a phase recorded if it already completed on the log, else None."""
    done = await bus.query({"trace_id": trace, "task_id": task_id, "type": ev.TASK_COMPLETED})
    if not done:
        return None
    value: dict | None = done[-1].payload.get("value")
    return value
