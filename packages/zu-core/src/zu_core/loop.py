"""The interpreter loop (build step 4).

The read-eval-print interpreter of the runtime: ask the provider for an
action, dispatch the tool by name, run detectors on the observation, repeat
until the model finalises or the budget is spent; on finalise, run the
validation ladder. It is provider-, tool-, and detector-agnostic — it only
knows the ports. The detector checkpoints are where escalation is decided.

This module is a typed scaffold. It is wired in build step 4 against the
ScriptedProvider, so the whole loop is tested offline before any real model.
"""

from __future__ import annotations

from .bus import EventBus
from .contracts import Result, TaskSpec
from .ports import ModelProvider
from .registry import Registry


async def run_task(
    spec: TaskSpec,
    provider: ModelProvider,
    registry: Registry,
    bus: EventBus | None = None,
) -> Result:
    """Drive one task to a Result. Implemented in build step 4."""
    raise NotImplementedError(
        "The interpreter loop is build step 4 — see zu_mlr_design §4.2. "
        "Build it against the ScriptedProvider so it is deterministic offline."
    )
