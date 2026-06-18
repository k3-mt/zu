"""The interpreter loop (build step 4).

The read-eval-print interpreter of the runtime: ask the provider for an action,
dispatch the tool by name, run detectors on the observation, repeat until the
model finalises or the budget is spent; on finalise, run the validation ladder.
It is provider-, tool-, and detector-agnostic — it only knows the ports and the
one registry. The detector checkpoints are where escalation is decided; the
model may signal an action, never acquire a capability itself.

Determinism (the step-4 promise): with the ``ScriptedProvider`` and a fixtured
tool, the loop produces the **same Result every run**. Event ids and timestamps
differ run-to-run by design, so determinism is asserted on the Result and the
*sequence* of event types, never on event ids.

Detectors and validators are pulled from the registry, so this loop already has
the checkpoints steps 5 (escalation) and 6 (validation) plug into — when none
are registered (the step-4 case) the checkpoints are inert.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

from . import events as ev
from .bus import EventBus
from .contracts import Event, Result, Status, TaskSpec
from .ports import (
    Finish,
    ModelProvider,
    ModelRequest,
    RunContext,
    Scope,
    Severity,
    Verdict,
)
from .registry import REGISTRY, Registry

log = logging.getLogger("zu.loop")

# Severity ordering for picking the worst verdict at a checkpoint.
_RANK = {Severity.WARN: 0, Severity.RETRY: 1, Severity.ESCALATE: 2, Severity.TERMINAL: 3}

# Observation keys that carry retrieved page content — stored once (in a
# data.source.fetched event), summarised in the harness.tool.returned event.
_CONTENT_KEYS = ("html", "text", "content")


def _materialize(obj: Any) -> Any:
    """Registry entries may be classes (entry-point discovery) or already-built
    instances (config in step 8, or a test). The loop needs a usable instance.

    Note: a discovered class is instantiated with no arguments, so a plugin
    that needs constructor config can only be used by registering an instance
    (the configured-instance path that build step 8 formalises)."""
    return obj() if isinstance(obj, type) else obj


def _usage_tokens(usage: dict) -> int:
    if not usage:
        return 0
    if "total_tokens" in usage:
        return int(usage["total_tokens"])
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


def _parse_value(text: str | None) -> dict | None:
    """Turn the model's final text into a structured value. A JSON object is
    used as-is; any other JSON or plain text is wrapped so the result is always
    a dict (what schema/grounding validation and the result contract expect)."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"text": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _summarize_observation(obs: dict) -> dict:
    """A compact view of an observation for the harness.tool.returned event:
    drop the large content fields (kept in full in data.source.fetched) and
    replace them with their length, so a fetched page isn't stored twice."""
    summary = {k: v for k, v in obs.items() if k not in _CONTENT_KEYS}
    for k in _CONTENT_KEYS:
        if isinstance(obs.get(k), str):
            summary[f"{k}_len"] = len(obs[k])
    return summary


def _worst(verdicts: list[Verdict]) -> Verdict | None:
    return max(verdicts, key=lambda v: _RANK[v.severity], default=None)


class _Run:
    """Per-run state: one trace id, the growing event list, and the emitter."""

    def __init__(self, spec: TaskSpec, bus: EventBus) -> None:
        self.spec = spec
        self.bus = bus
        self.trace_id = spec.task_id  # one trace per task in the v1 runtime
        self.task_id = spec.task_id
        # One RunContext reused for the whole run: its ``events`` list is the
        # live log (appended in place by emit) and ``observation`` is updated
        # per checkpoint — so a checkpoint is O(1), not an O(n) copy of the log.
        self._ctx = RunContext(spec=spec, observation=None, events=[])
        self.events: list[Event] = self._ctx.events
        self.root = None  # event_id of TASK_STARTED; parent of terminal events

    async def emit(self, type_: str, payload: dict | None = None, *, parent=None, source="loop"):
        event = Event(
            trace_id=self.trace_id,
            task_id=self.task_id,
            parent_id=parent,
            type=type_,
            source=source,
            payload=payload or {},
        )
        await self.bus.publish(event)
        self.events.append(event)
        return event.event_id

    def ctx(self, observation: Any = None) -> RunContext:
        # Reuse the single context object; just point it at the current
        # observation. Detectors/validators read it as a read-only view.
        self._ctx.observation = observation
        return self._ctx

    async def terminal(self, reason: str) -> Result:
        await self.emit(ev.TASK_TERMINAL, {"reason": reason}, parent=self.root)
        return Result(status=Status.TERMINAL, reason=reason)

    async def escalate(self, reason: str) -> Result:
        await self.emit(ev.TASK_ESCALATED, {"reason": reason}, parent=self.root)
        return Result(status=Status.ESCALATE, reason=reason)


async def run_task(
    spec: TaskSpec,
    provider: ModelProvider,
    registry: Registry | None = None,
    bus: EventBus | None = None,
) -> Result:
    """Drive one task to a Result against the given provider and registry.

    ``registry`` defaults to the process-wide ``REGISTRY`` (so decorator- and
    entry-point-registered plugins are both visible); pass an explicit one to
    isolate. Budgets are *soft* — token and wall-time limits are enforced
    between turns, so a single turn may overshoot before the run is ended; a
    hard per-call token cap arrives with the real providers (build step 7).
    """
    registry = registry if registry is not None else REGISTRY
    bus = bus or EventBus()
    run = _Run(spec, bus)
    budget = spec.budget

    tools = {name: _materialize(registry.get("tools", name)) for name in registry.names("tools")}
    detectors = [_materialize(registry.get("detectors", n)) for n in registry.names("detectors")]
    validators = [_materialize(registry.get("validators", n)) for n in registry.names("validators")]

    run.root = await run.emit(ev.TASK_STARTED, {"query": spec.query, "target": spec.target})

    messages = _initial_messages(spec, tools.values())
    tool_schemas = [t.schema for t in tools.values() if getattr(t, "schema", None)]

    start = time.monotonic()
    tokens = 0

    for step in range(budget.max_steps):
        # --- budget checkpoints (time / tokens) before spending a model call ---
        if time.monotonic() - start > budget.wall_time_s:
            return await run.terminal("budget:wall_time_s")
        if tokens > budget.max_tokens:
            return await run.terminal("budget:max_tokens")

        turn = await run.emit(ev.TURN_STARTED, {"step": step + 1}, parent=run.root)
        resp = await provider.complete(ModelRequest(messages=messages, tools=tool_schemas))
        tokens += _usage_tokens(resp.usage)
        # Re-check after the call so a turn that itself overshoots is caught,
        # not just a subsequent one.
        if tokens > budget.max_tokens:
            return await run.terminal("budget:max_tokens")
        if time.monotonic() - start > budget.wall_time_s:
            return await run.terminal("budget:wall_time_s")

        # --- the model chose actions: dispatch tools, then detector checkpoints ---
        if resp.tool_calls:
            if len(resp.tool_calls) > budget.max_tool_calls:
                return await run.terminal("budget:max_tool_calls")
            messages.append(
                {
                    "role": "assistant",
                    "tool_calls": [{"name": c.name, "args": c.args} for c in resp.tool_calls],
                }
            )
            for call in resp.tool_calls:
                obs = await _invoke(run, turn, tools, call.name, call.args)
                messages.append(
                    {"role": "tool", "name": call.name, "content": json.dumps(obs, default=str)}
                )
                halt = await _detector_checkpoint(run, turn, detectors, obs, {Scope.PER_OBSERVATION})
                if halt is not None:
                    return halt
            halt = await _detector_checkpoint(run, turn, detectors, None, {Scope.PER_TURN})
            if halt is not None:
                return halt
            continue

        # --- the model finalised: validate, then complete / retry / halt ---
        if resp.finish == Finish.LENGTH:
            return await run.terminal("model truncated (length)")
        value = _parse_value(resp.text)
        if value is None:
            return await run.terminal("model finalised with no answer")

        candidate = Result(status=Status.SUCCESS, value=value)
        verdict = _finalise_verdict(run, detectors, validators, candidate)
        if verdict is not None:
            await run.emit(
                ev.VALIDATION_FAILED,
                {"detector": verdict.detector, "severity": verdict.severity.value, "detail": verdict.detail},
                parent=run.root,
                source=verdict.detector,
            )
            if verdict.severity == Severity.TERMINAL:
                return await run.terminal(verdict.detector)
            if verdict.severity == Severity.ESCALATE:
                return await run.escalate(verdict.detector)
            if verdict.severity == Severity.RETRY:
                # feed the failure back and let the model correct (next turn,
                # bounded by the step budget); WARN falls through to success.
                messages.append(
                    {
                        "role": "user",
                        "content": f"Validation failed ({verdict.detector}): "
                        f"{verdict.detail}. Correct the output and resubmit.",
                    }
                )
                continue

        await run.emit(ev.RECORD_EXTRACTED, {"value": value}, parent=run.root)
        await run.emit(ev.TASK_COMPLETED, {"value": value}, parent=run.root)
        return candidate

    return await run.terminal("budget:max_steps")


def _initial_messages(spec: TaskSpec, tools: Iterable[Any]) -> list[dict]:
    fragments = "\n".join(
        f"- {t.prompt_fragment}" for t in tools if getattr(t, "prompt_fragment", None)
    )
    system = "You are Zu, a tool-using agent. Return the final answer as a single JSON object."
    if fragments:
        system = (
            "You are Zu, a tool-using agent. Use the available tools to answer "
            "the task, then return the final answer as a single JSON object.\n"
            f"Available tools:\n{fragments}"
        )
    user = spec.query if not spec.target else f"{spec.query}\nTarget: {spec.target}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _invoke(run: _Run, turn, tools: dict, name: str, args: dict) -> dict:
    """Dispatch one tool call to an observation. A missing tool or a raising
    tool (e.g. an SSRF block) becomes an error observation, never a crash —
    the same isolation principle the bus applies to subscribers. Unexpected
    failures are logged so a real bug isn't silently disguised as data."""
    await run.emit(ev.TOOL_INVOKED, {"tool": name, "args": args}, parent=turn, source=name)
    tool = tools.get(name)
    if tool is None:
        obs: dict = {"error": f"unknown tool: {name}"}
    else:
        try:
            obs = await tool(run.ctx(), **args)
        except Exception as exc:  # noqa: BLE001 - tool failure is an observation
            log.warning("tool %r raised %s: %s", name, type(exc).__name__, exc)
            obs = {"error": f"{type(exc).__name__}: {exc}"}
    # When the observation carried retrieved content, store it once in a data
    # event (the provenance grounding reads in step 6) and summarise it in the
    # tool.returned event, so a fetched page isn't duplicated in the log. The
    # check is on content *shape*, not the tool's name — the loop stays
    # tool-agnostic.
    if isinstance(obs, dict) and any(k in obs for k in _CONTENT_KEYS):
        await run.emit(ev.SOURCE_FETCHED, obs, parent=turn, source=name)
        returned = _summarize_observation(obs)
    else:
        returned = obs
    await run.emit(ev.TOOL_RETURNED, {"tool": name, "observation": returned}, parent=turn, source=name)
    return obs


async def _detector_checkpoint(
    run: _Run, turn, detectors: list, observation: Any, scopes: set[Scope]
) -> Result | None:
    """Run the in-scope detectors; emit DETECTOR_FIRED for any verdict. Only
    ESCALATE / TERMINAL halt the loop — RETRY/WARN are recorded and the run
    continues (the model sees the observation and decides)."""
    ctx = run.ctx(observation)
    for d in detectors:
        if getattr(d, "scope", None) not in scopes:
            continue
        verdict = d.inspect(ctx)
        if verdict is None:
            continue
        await run.emit(
            ev.DETECTOR_FIRED,
            {"detector": verdict.detector, "severity": verdict.severity.value, "detail": verdict.detail},
            parent=turn,
            source=verdict.detector,
        )
        if verdict.severity == Severity.ESCALATE:
            return await run.escalate(verdict.detector)
        if verdict.severity == Severity.TERMINAL:
            return await run.terminal(verdict.detector)
    return None


def _finalise_verdict(
    run: _Run, detectors: list, validators: list, candidate: Result
) -> Verdict | None:
    """The ON_FINAL ladder: ON_FINAL detectors then validators. Returns the
    single worst verdict (or None if everything passed)."""
    ctx = run.ctx()
    verdicts: list[Verdict] = []
    for d in detectors:
        if getattr(d, "scope", None) == Scope.ON_FINAL:
            v = d.inspect(ctx)
            if v is not None:
                verdicts.append(v)
    for val in validators:
        v = val.check(candidate, ctx)
        if v is not None:
            verdicts.append(v)
    return _worst(verdicts)
