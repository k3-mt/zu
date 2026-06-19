"""The interpreter loop (build steps 4–5).

The read-eval-print interpreter of the runtime: ask the provider for an action,
dispatch the tool by name, run detectors on the observation, repeat until the
model finalises or the budget is spent; on finalise, run the validation ladder.
It is provider-, tool-, and detector-agnostic — it only knows the ports and the
one registry. The detector checkpoints are where escalation is decided; the
model may signal an action, never acquire a capability itself.

The tier ladder (build step 5): tools carry a ``tier``, and the loop only
offers the model the tools at or below the run's current tier — tier 1
(``http_fetch``) to start. A detector ESCALATE is not the end of the run; it is
a *step* that climbs one tier, unlocking a higher-capability tool (a browser via
``render_dom``) and letting the model retry the same job. The run only ends with
an ESCALATE Result when there is no higher tier left to climb to.

Determinism (the step-4 promise): with the ``ScriptedProvider`` and a fixtured
tool, the loop produces the **same Result every run**. Event ids and timestamps
differ run-to-run by design, so determinism is asserted on the Result and the
*sequence* of event types, never on event ids.

Detectors and validators are pulled from the registry, so the checkpoints are
inert when none are registered (the step-4 case).
"""

from __future__ import annotations

import json
import re
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
    declared_envelope,
)
from .registry import REGISTRY, Registry
from .security import SecurityBlock

log = logging.getLogger("zu.loop")

# Severity ordering for picking the worst verdict at a checkpoint.
_RANK = {Severity.WARN: 0, Severity.RETRY: 1, Severity.ESCALATE: 2, Severity.TERMINAL: 3}

# Observation keys that carry retrieved page content — stored once (in a
# data.source.fetched event), summarised in the harness.tool.returned event.
_CONTENT_KEYS = ("html", "text", "content")

# Hard cap on a single tool observation's serialized size. A hostile tool can
# return an enormous, deeply-nested, or shared-reference ("schema bomb")
# structure that explodes when serialized (to the model message and to the event
# log) and OOMs the harness. We reject it gracefully instead — the secure-by-
# default claim that "parsing and size limits reject it" made real.
_MAX_OBSERVATION_BYTES = 1_000_000


def _within_size(obj: Any, max_bytes: int = _MAX_OBSERVATION_BYTES) -> bool:
    """True iff ``obj`` serializes to <= max_bytes of JSON — checked WITHOUT
    materializing a pathological structure. ``iterencode`` yields lazily, so an
    exponential/shared-reference bomb is caught after the first max_bytes of
    output rather than after full (2^depth) expansion; a circular reference
    raises ValueError and is likewise rejected."""
    total = 0
    try:
        for chunk in json.JSONEncoder(default=str).iterencode(obj):
            total += len(chunk)
            if total > max_bytes:
                return False
    except ValueError:
        return False
    return True


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


_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*\n?(.*?)\n?```$", re.DOTALL)


def _parse_value(text: str | None) -> dict | None:
    """Turn the model's final text into a structured value. A JSON object is
    used as-is; any other JSON or plain text is wrapped so the result is always
    a dict (what schema/grounding validation and the result contract expect).

    Real models routinely wrap JSON in a markdown code fence (```json … ```);
    strip a single enclosing fence before parsing so that common output isn't
    treated as opaque text (which would fail grounding and waste retries)."""
    if not text:
        return None
    candidate = text.strip()
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        parsed = json.loads(candidate)
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


def _tier_of(tool: Any) -> int:
    """A tool's tier; defaults to 1 so a tool that omits it is the cheap tier."""
    return int(getattr(tool, "tier", 1))


class _Ladder:
    """The escalation ladder: which tools are offered at the current tier, and
    the climb a detector ESCALATE triggers.

    The ceiling is the *lower* of the task's ``max_tier`` and the highest tier
    any registered tool actually occupies — so the loop never climbs to an
    empty tier (which would just re-offer the same tools and escalate again).
    With only tier-1 tools registered, the ceiling is 1 and an ESCALATE has
    nowhere to climb: it ends the run, which is the step-4 behaviour preserved.
    """

    def __init__(self, tools: dict[str, Any], max_tier: int) -> None:
        self._all = tools
        self.current = 1
        top = max((_tier_of(t) for t in tools.values()), default=1)
        self.ceiling = min(max_tier, top)

    def active(self) -> dict[str, Any]:
        """The tools the model may use right now: tier <= current tier."""
        return {n: t for n, t in self._all.items() if _tier_of(t) <= self.current}

    def schemas(self) -> list[dict]:
        return [t.schema for t in self.active().values() if getattr(t, "schema", None)]

    @property
    def can_climb(self) -> bool:
        return self.current < self.ceiling

    def climb(self) -> int:
        self.current += 1
        return self.current


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

    async def escalate(self, reason: str, tier: int) -> Result:
        # Escalation with no higher tier to climb to: the run ends ESCALATE.
        # ``exhausted`` distinguishes this terminal event from the climb event
        # (which carries from_tier/to_tier) on the same TASK_ESCALATED type.
        await self.emit(
            ev.TASK_ESCALATED, {"reason": reason, "tier": tier, "exhausted": True}, parent=self.root
        )
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

    # The tier ladder gates which tools the model sees; the run starts at tier 1.
    ladder = _Ladder(tools, spec.max_tier)

    run.root = await run.emit(ev.TASK_STARTED, {"query": spec.query, "target": spec.target})

    # Record each tool's declared capability envelope onto the log at run start,
    # so the out-of-band verdict observers (the gate, and the always-on runtime
    # checks) can judge observed behaviour against what each plugin declared.
    await run.emit(
        ev.ENVELOPE_DECLARED,
        {
            "tools": {
                name: {"tier": _tier_of(t), **declared_envelope(t)}
                for name, t in tools.items()
            }
        },
        parent=run.root,
    )

    messages = _initial_messages(spec, ladder.active().values())

    start = time.monotonic()
    tokens = 0

    for step in range(budget.max_steps):
        # --- budget checkpoints (time / tokens) before spending a model call ---
        if time.monotonic() - start > budget.wall_time_s:
            return await run.terminal("budget:wall_time_s")
        if tokens >= budget.max_tokens:
            return await run.terminal("budget:max_tokens")

        # Recompute per turn so a tier climbed last turn takes effect now: the
        # model is offered exactly the tools unlocked at the current tier.
        active = ladder.active()
        tool_schemas = ladder.schemas()

        turn = await run.emit(ev.TURN_STARTED, {"step": step + 1}, parent=run.root)
        resp = await provider.complete(ModelRequest(messages=messages, tools=tool_schemas))
        tokens += _usage_tokens(resp.usage)
        # Record this call's usage, tier, and model into the log so cost is
        # reconstructable after the fact (a read-side projection sums these);
        # ``model`` is whatever the provider exposes (None for the fake one).
        await run.emit(
            ev.TURN_COMPLETED,
            {
                "step": step + 1,
                "tier": ladder.current,
                "model": getattr(provider, "model", None),
                "usage": dict(resp.usage),
                # The model's natural-language output this turn — its "train of
                # thought" (a plan/explanation before a tool call, or the final
                # answer). Surfaced so a live trace can show *why*, not just what.
                "text": resp.text,
            },
            parent=turn,
        )
        # Re-check after the call so a turn that itself overshoots is caught,
        # not just a subsequent one.
        if tokens >= budget.max_tokens:
            return await run.terminal("budget:max_tokens")
        if time.monotonic() - start > budget.wall_time_s:
            return await run.terminal("budget:wall_time_s")

        # A truncated response is unusable whether it finalised OR called tools:
        # tool-call arguments cut off mid-generation are exactly the malformed
        # untrusted output we must not dispatch. Check before acting on anything.
        if resp.finish == Finish.LENGTH:
            return await run.terminal("model truncated (length)")

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
            halting: Verdict | None = None
            for call in resp.tool_calls:
                obs = await _invoke(run, turn, active, call.name, call.args)
                messages.append(
                    {"role": "tool", "name": call.name, "content": json.dumps(obs, default=str)}
                )
                halting = await _detector_checkpoint(run, turn, detectors, obs, {Scope.PER_OBSERVATION})
                if halting is not None:
                    break  # stop dispatching this turn's remaining calls; act on it
            if halting is None:
                halting = await _detector_checkpoint(run, turn, detectors, None, {Scope.PER_TURN})
            if halting is not None:
                if halting.severity == Severity.TERMINAL:
                    return await run.terminal(halting.detector)
                # ESCALATE: climb a tier (loop continues) or end the run.
                halt = await _escalate(run, ladder, messages, halting)
                if halt is not None:
                    return halt
            continue

        # --- the model finalised: validate, then complete / retry / halt ---
        # (truncation is already handled above, before any dispatch.)
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
                # An on-final escalation climbs too: unlock the higher tier and
                # let the model retry, rather than ending on the first failure.
                halt = await _escalate(run, ladder, messages, verdict)
                if halt is not None:
                    return halt
                continue
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
    failures are logged so a real bug isn't silently disguised as data.

    ``tools`` is the *active* set for the current tier, so a call to a tool
    that hasn't been unlocked yet falls into the unknown-tool branch — the
    ladder is enforced on dispatch, not just on what the model is shown."""
    await run.emit(ev.TOOL_INVOKED, {"tool": name, "args": args}, parent=turn, source=name)
    tool = tools.get(name)
    if tool is None:
        obs: dict = {"error": f"unknown tool: {name}"}
    else:
        try:
            obs = await tool(run.ctx(), **args)
        except SecurityBlock as block:
            # A guard contained the action (e.g. an SSRF/egress refusal). Record
            # it as a defense so the blocked attempt is on the log, then surface
            # it to the model as an error observation like any other failure.
            await run.emit(
                ev.DEFENSE_BLOCKED,
                {"kind": block.kind, "tool": name, "target": block.target, "detail": str(block)},
                parent=turn,
                source=name,
            )
            log.warning("tool %r blocked (%s): %s", name, block.kind, block)
            obs = {"error": f"{type(block).__name__}: {block}", "blocked": block.kind}
        except Exception as exc:  # noqa: BLE001 - tool failure is an observation
            log.warning("tool %r raised %s: %s", name, type(exc).__name__, exc)
            obs = {"error": f"{type(exc).__name__}: {exc}"}
        # Reject an oversized/unserialisable observation before it is stored or
        # forwarded — a schema bomb is contained here, not after it has OOMed.
        if not _within_size(obs):
            await run.emit(
                ev.DEFENSE_BLOCKED,
                {"kind": "oversized_observation", "tool": name,
                 "detail": "tool observation exceeds the size limit and was rejected"},
                parent=turn,
                source=name,
            )
            log.warning("tool %r returned an oversized observation; rejecting it", name)
            obs = {"error": "tool observation exceeds the size limit and was rejected",
                   "blocked": "oversized_observation"}
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
) -> Verdict | None:
    """Run every in-scope detector, emit DETECTOR_FIRED for each verdict, and
    return the *worst* halting verdict (ESCALATE or TERMINAL) for the caller to
    act on — TERMINAL ends the run, ESCALATE climbs a tier. RETRY/WARN are
    recorded and the run continues (the model sees the observation and decides).

    Picking the worst (not the first) matters: detectors run in registry order,
    so a page that is both fatal and escalatable — e.g. a 404 with an empty body
    firing both ``error`` (TERMINAL) and ``empty`` (ESCALATE) — must terminate,
    never waste a tier climb just because ``empty`` happened to sort first. This
    mirrors the ON_FINAL ladder, which already takes the worst verdict."""
    ctx = run.ctx(observation)
    verdicts: list[Verdict] = []
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
        verdicts.append(verdict)
    worst = _worst(verdicts)
    if worst is not None and worst.severity in (Severity.ESCALATE, Severity.TERMINAL):
        return worst
    return None


async def _escalate(
    run: _Run, ladder: _Ladder, messages: list[dict], verdict: Verdict
) -> Result | None:
    """Act on an ESCALATE verdict. Climb one tier if there is headroom — emit
    the escalation step, unlock the higher tier, and tell the model what is now
    available — returning None so the loop retries the job. With no tier left to
    climb to, end the run with an ESCALATE Result (the reason is the detector)."""
    if not ladder.can_climb:
        return await run.escalate(verdict.detector, ladder.current)
    frm = ladder.current
    to = ladder.climb()
    await run.emit(
        ev.TASK_ESCALATED,
        {"reason": verdict.detector, "detail": verdict.detail, "from_tier": frm, "to_tier": to},
        parent=run.root,
        source=verdict.detector,
    )
    messages.append({"role": "user", "content": _escalation_notice(to, ladder.active(), verdict)})
    return None


def _escalation_notice(tier: int, active: dict, verdict: Verdict) -> str:
    """Tell the model the previous tier was insufficient and which tools the
    climb just unlocked, so a real model retries with the new capability. The
    ScriptedProvider ignores it; the wording is for the step-7 providers."""
    unlocked = "\n".join(
        f"- {t.prompt_fragment}"
        for t in active.values()
        if _tier_of(t) == tier and getattr(t, "prompt_fragment", None)
    )
    return (
        f"The previous attempt was insufficient ({verdict.detector}: {verdict.detail}). "
        f"Escalated to tier {tier}. Newly available tools:\n{unlocked}\n"
        "Retry the task using them."
    )


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
