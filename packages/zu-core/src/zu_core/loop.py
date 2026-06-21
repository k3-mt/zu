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

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

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
from .security import SecurityBlock, enforce_containment
from .track import MAX_REPLAY_WAIT_MS, Track

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


def _as_int(value: Any) -> int:
    """Coerce a usage field to int, tolerating a missing or malformed value.

    A provider's usage dict is semi-trusted adapter output; a non-numeric or
    ``None`` token count must not crash the loop mid-run (the budget simply sees
    zero for that field rather than raising ``TypeError``/``ValueError``)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _usage_tokens(usage: dict) -> int:
    if not usage:
        return 0
    if "total_tokens" in usage:
        return _as_int(usage["total_tokens"])
    return _as_int(usage.get("input_tokens")) + _as_int(usage.get("output_tokens"))


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


# Map-reduce extraction over a too-big page. A blunt cap keeps the first N chars
# and drops the rest — if the wanted data is past the cap it is lost. The extract
# strategy instead scans the WHOLE page in context-sized chunks, pulling the parts
# relevant to the task out of each (the map), and feeds the combined extract to the
# loop (the reduce). Costs one utility model call per chunk, bounded below.
_MAX_EXTRACT_CHUNKS = 16  # ceiling on map calls per field, so a huge page can't run away

_EXTRACT_PROMPT = (
    "You are extracting the relevant parts of ONE fragment of a larger web page, "
    "to help with this task:\n{query}\n\n"
    "Fragment (part {i} of {n}):\n{chunk}\n\n"
    "Copy out — verbatim — only the parts of THIS fragment relevant to the task: "
    "keep exact values (dates, times, prices, names, URLs) and the text around them; "
    "drop navigation, ads, scripts, and boilerplate. If nothing in this fragment is "
    "relevant, reply with exactly: NOTHING"
)


async def _extract_relevant(
    content: str, query: str, provider: ModelProvider, max_chars: int
) -> str:
    """Map-reduce a too-big ``content`` down to what matters for ``query``.

    Split into ``max_chars``-sized chunks, ask ``provider`` to pull the relevant
    text from each (concise, verbatim), and join the non-empty results. The full
    original is untouched on the event log, so grounding still verifies the final
    answer against the real page — the extract only shapes what the model reads."""
    chunks = [content[i : i + max_chars] for i in range(0, len(content), max_chars)]
    dropped = max(0, len(chunks) - _MAX_EXTRACT_CHUNKS)
    chunks = chunks[:_MAX_EXTRACT_CHUNKS]
    extracts: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        prompt = _EXTRACT_PROMPT.format(query=query or "(no specific task)", i=i, n=len(chunks), chunk=chunk)
        try:
            resp = await provider.complete(
                ModelRequest(messages=[{"role": "user", "content": prompt}], tools=[])
            )
            text = (resp.text or "").strip()
        except Exception:  # noqa: BLE001 - a failed map call drops that chunk, never crashes the run
            text = ""
        if text and text.upper() != "NOTHING":
            extracts.append(text)
    combined = "\n\n".join(extracts) if extracts else "(no content relevant to the task was found on the page)"
    if dropped:
        combined += f"\n…[{dropped} further chunk(s) of the page were not scanned]"
    if len(combined) > max_chars:  # backstop: the extract itself must fit the budget
        combined = combined[:max_chars] + "\n…[extract truncated]"
    return combined


def _bounded_history(messages: list[dict], max_chars: int | None, *, keep_recent: int = 3) -> list[dict]:
    """Keep the running conversation within ``max_chars`` by eliding the content of
    OLD tool observations — the big, stale part of a long agentic run.

    Per-observation capping bounds one tool result; this bounds their SUM across a
    long multi-step run (e.g. driving a browser open→act→read… for many turns),
    which otherwise grows until it overflows the model's context window. The system
    prompt, the task, every assistant turn (the model's own notes/decisions), and
    the most recent ``keep_recent`` tool results are kept verbatim; older tool
    results are replaced with a short stub that POINTS AT ``recall`` — the content
    is still on the event log (elided here, not lost), so the model can query it
    back in chunks rather than the data being dropped. Off (None) → unchanged."""
    if max_chars is None:
        return messages
    total = sum(len(m.get("content") or "") for m in messages)
    if total <= max_chars:
        return messages
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    elidable = tool_idxs[:-keep_recent] if keep_recent else tool_idxs
    out = list(messages)
    for i in elidable:
        content = out[i].get("content") or ""
        if len(content) <= 200 or content.startswith("[elided"):
            continue
        out[i] = {**out[i],
                  "content": f"[elided {len(content)} chars of an earlier "
                             f"{out[i].get('name', 'tool')} result to fit the context window — "
                             "still on the run log; use recall(<keyword>) to retrieve it]"}
        total -= len(content)
        if total <= max_chars:
            break
    return out


async def _shrink_for_model(
    obs: Any, *, max_chars: int | None, strategy: str, provider: ModelProvider, query: str
) -> Any:
    """Shape a tool observation to fit the model's context, per the configured
    strategy. ``truncate`` (cheap, no calls) keeps the head; ``extract`` map-reduces
    the whole page to the task-relevant parts (costs model calls). Off (``max_chars``
    None) returns the observation untouched. Only content fields are shaped."""
    if max_chars is None or not isinstance(obs, dict):
        return obs
    if strategy != "extract":
        return _observation_for_model(obs, max_chars)
    capped = dict(obs)
    for k in _CONTENT_KEYS:
        v = capped.get(k)
        if isinstance(v, str) and len(v) > max_chars:
            capped[k] = await _extract_relevant(v, query, provider, max_chars)
    return capped


def _observation_for_model(obs: Any, max_chars: int | None) -> Any:
    """Optionally bound large content fields of an observation before it enters the
    model's message history — LOSSLESSLY.

    OFF by default (``max_chars`` None) — the model sees the full observation, so a
    large-context model keeps everything. It is an OPT-IN policy
    (``max_observation_chars``) for agents that fetch big pages on a small-context
    model. When set, an over-budget content field is NOT truncated (which would
    silently drop the tail the model needs) — it is ELIDED to a ``recall`` pointer:
    the FULL content stays on the event log (``data.source.fetched``, which
    grounding reads and ``recall`` queries), so the model pulls back exactly the
    part it needs on demand instead of having the whole thing dumped (or its tail
    cut) every turn. Non-dict observations pass through unchanged."""
    if max_chars is None or not isinstance(obs, dict):
        return obs
    capped = dict(obs)
    for k in _CONTENT_KEYS:
        v = capped.get(k)
        if isinstance(v, str) and len(v) > max_chars:
            capped[k] = (
                f"[{len(v)} chars of retrieved content elided to keep the context lean — "
                "it is on the run log; use recall(<keyword>) to read the part you need]"
            )
    return capped


def _worst(verdicts: list[Verdict]) -> Verdict | None:
    return max(verdicts, key=lambda v: _RANK[v.severity], default=None)


def _budget_reason(elapsed: float, tokens: int, budget: Any) -> str | None:
    """The terminal reason if a budget bound is exceeded, else None.

    Wall-time and tokens are checked together so the two control-plane bounds
    have one definition: this is called before spending a model call and again
    after, so a turn that itself overshoots is caught, not only a later one."""
    if elapsed > budget.wall_time_s:
        return "budget:wall_time_s"
    if tokens >= budget.max_tokens:
        return "budget:max_tokens"
    return None


class _EventsView(Sequence):
    """A read-only window onto the live event log handed to plugins.

    It wraps the loop's event list *by reference*, so it reflects the log as it
    grows with no per-checkpoint copy, but a detector/validator/tool cannot
    append, replace, or delete records through it — the canonical log stays the
    loop's alone. ``RunContext.events`` holds this view, not the raw list."""

    __slots__ = ("_events",)

    def __init__(self, events: list) -> None:
        self._events = events

    def __getitem__(self, index: Any) -> Any:
        return self._events[index]

    def __len__(self) -> int:
        return len(self._events)


def _safe_inspect(detector: Any, ctx: RunContext) -> Verdict | None:
    """Run a detector in isolation: a raising third-party detector is logged and
    skipped, never allowed to crash the run — the same isolation the bus gives
    its subscribers and the loop gives its tools."""
    try:
        verdict: Verdict | None = detector.inspect(ctx)
        return verdict
    except Exception as exc:  # noqa: BLE001 - a broken detector must not halt the run
        log.warning(
            "detector %r raised %s: %s — skipping it",
            getattr(detector, "name", detector), type(exc).__name__, exc,
        )
        return None


def _safe_check(validator: Any, result: Result, ctx: RunContext) -> Verdict | None:
    """Run a validator in isolation: a raising validator is logged and skipped,
    so a buggy third-party validator cannot take down every run."""
    try:
        verdict: Verdict | None = validator.check(result, ctx)
        return verdict
    except Exception as exc:  # noqa: BLE001 - a broken validator must not halt the run
        log.warning(
            "validator %r raised %s: %s — skipping it",
            getattr(validator, "name", validator), type(exc).__name__, exc,
        )
        return None


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

    def __init__(self, spec: TaskSpec, bus: EventBus, trace_id: UUID | None = None) -> None:
        self.spec = spec
        self.bus = bus
        # ``trace_id`` correlates a run's events. It defaults to the task id (one
        # trace per task), but a multi-phase pipeline passes a shared id so every
        # phase's events fold into one replayable lineage (see zu.Pipeline). The
        # per-phase ``task_id`` stays distinct, so a phase is still queryable alone.
        self.trace_id = trace_id if trace_id is not None else spec.task_id
        self.task_id = spec.task_id
        # One RunContext reused for the whole run: ``observation`` is updated
        # per checkpoint — so a checkpoint is O(1), not an O(n) copy of the log.
        # Plugins (tools/detectors/validators) receive ``ctx.events`` as a
        # *read-only window* onto the live log: it reflects the log as it grows
        # (no copy) but a misbehaving plugin cannot mutate or corrupt the
        # canonical record through it. The loop appends to ``self.events``.
        self.events: list[Event] = []
        self._ctx = RunContext(spec=spec, observation=None, events=[])
        self._ctx.events = _EventsView(self.events)
        self.root: UUID | None = None  # event_id of TASK_STARTED; parent of terminal events

    async def emit(
        self, type_: str, payload: dict | None = None, *,
        parent: UUID | None = None, source: str = "loop",
    ) -> UUID:
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


def _is_challenge(obs: Any) -> bool:
    """Did a replayed step hit a challenge — i.e. diverge from the recorded path so
    the model must take over? A tool error, a blocked/refused call, an action that
    missed (action_error), or an HTTP error status. (A successful step that merely
    returns different live DATA is NOT a challenge — the navigation still worked.)"""
    if not isinstance(obs, dict):
        return False
    if obs.get("error") or obs.get("action_error") or obs.get("blocked"):
        return True
    status = obs.get("status")
    return isinstance(status, int) and status >= 400


async def _replay_track(
    run: _Run, track: Track, tools: dict, messages: list[dict], *,
    wall_time_s: float, start: float, max_observation_chars: int | None,
) -> bool:
    """Drive the recorded path deterministically — re-issue each tool call in order,
    with NO model call — appending the same assistant/tool message pair the loop
    would, so the model has consistent history if it takes over. Stops (returning
    True) at the first challenge; returns False when the whole track replayed. Paced
    by the recorded gaps (capped), and bounded by the run's wall-time."""
    for i, step in enumerate(track.steps):
        if wall_time_s - (time.monotonic() - start) <= 0:
            return True  # out of time; let the model loop end the run cleanly
        if step.wait_ms:
            await asyncio.sleep(min(step.wait_ms, MAX_REPLAY_WAIT_MS) / 1000)
        turn = await run.emit(ev.TURN_STARTED, {"step": i + 1, "replay": True}, parent=run.root)
        remaining = max(0.0, wall_time_s - (time.monotonic() - start))
        obs = await _invoke(run, turn, tools, step.tool, step.args, timeout=remaining)
        messages.append(
            {"role": "assistant", "content": f"(replay step {i + 1})",
             "tool_calls": [{"name": step.tool, "args": step.args}]}
        )
        messages.append(
            {"role": "tool", "name": step.tool,
             "content": json.dumps(_observation_for_model(obs, max_observation_chars), default=str)}
        )
        if _is_challenge(obs):
            return True  # diverged — hand the frontier to the model from here
    return False


async def run_task(
    spec: TaskSpec,
    provider: ModelProvider,
    registry: Registry | None = None,
    bus: EventBus | None = None,
    *,
    providers: Mapping[int, ModelProvider] | None = None,
    containment: str = "audit",
    trace_id: UUID | None = None,
    max_observation_chars: int | None = None,
    observation_strategy: str = "truncate",
    max_context_chars: int | None = None,
    track: Track | None = None,
) -> Result:
    """Drive one task to a Result against the given provider and registry.

    ``provider`` is the run's **global** model provider, used on every tier unless
    a tier is overridden. ``providers`` is an optional per-tier override map
    (``{tier: ModelProvider}``): when the ladder climbs to a tier present in the
    map, the loop switches to that provider mid-run — the neutral message format
    lets a different adapter pick up the same conversation, so a cheap/fast model
    can do the tier-1 work and a frontier/vision model take over on escalation.
    A tier with no override falls back to the global ``provider``.

    ``registry`` defaults to the process-wide ``REGISTRY`` (so decorator- and
    entry-point-registered plugins are both visible); pass an explicit one to
    isolate. Wall-time is enforced *both* between turns and as a hard timeout on
    each model call (``asyncio.wait_for``), so a hung or runaway provider cannot
    overrun the deadline. The token budget remains soft — a single turn may
    overshoot ``max_tokens`` before the run is ended — until the real providers
    pass a remaining-token limit into the call (build step 7).

    ``containment`` is the fail-closed floor (see ``zu_core.security``): with
    ``"required"``, a tool with off-box reach is refused unless the run is inside
    the Zu sandbox; ``"audit"`` (default) runs in-process and logs declarations.
    The check runs *before* any tool is built or dispatched, so an uncontained
    capability tool never executes even once.
    """
    by_tier: Mapping[int, ModelProvider] = providers or {}
    registry = registry if registry is not None else REGISTRY
    bus = bus or EventBus()
    run = _Run(spec, bus, trace_id=trace_id)
    budget = spec.budget

    tools = {name: _materialize(registry.get("tools", name)) for name in registry.names("tools")}
    detectors = [_materialize(registry.get("detectors", n)) for n in registry.names("detectors")]
    validators = [_materialize(registry.get("validators", n)) for n in registry.names("validators")]

    # Fail-closed containment floor: refuse before anything runs if a tool needs a
    # sandbox we're not inside. Raised (not a Result) — a misconfigured posture is
    # an operator error, surfaced loudly like a bad config, not a task outcome.
    enforce_containment(containment, tools)

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

    # --- replay a recorded track first (the navigator): drive the model's known
    # path deterministically, with NO model calls, until a step challenges (errors)
    # or the track runs out. The model loop below then takes over at that frontier,
    # sharing the same tools (and live browser session) and message history.
    if track is not None and track.matches(spec.query) and track.steps:
        await _replay_track(run, track, tools, messages,
                            wall_time_s=budget.wall_time_s, start=start,
                            max_observation_chars=max_observation_chars)
        # The replayed path used whatever tools it needed (already validated), so
        # unlock the full ladder for the model that continues from here.
        ladder.current = ladder.ceiling

    for step in range(budget.max_steps):
        # --- budget checkpoints (time / tokens) before spending a model call ---
        reason = _budget_reason(time.monotonic() - start, tokens, budget)
        if reason is not None:
            return await run.terminal(reason)

        # Recompute per turn so a tier climbed last turn takes effect now: the
        # model is offered exactly the tools unlocked at the current tier, and
        # the provider bound to that tier takes over (global provider otherwise).
        active = ladder.active()
        tool_schemas = ladder.schemas()
        turn_provider = by_tier.get(ladder.current, provider)

        turn = await run.emit(ev.TURN_STARTED, {"step": step + 1}, parent=run.root)
        # Bound the single model call by the wall-time the run has left: budgets
        # are otherwise only checked *between* turns, so a hung or runaway
        # provider could block forever and defeat ``wall_time_s`` entirely. A
        # timeout ends the run the same as any other wall-time exhaustion.
        remaining = budget.wall_time_s - (time.monotonic() - start)
        if remaining <= 0:
            return await run.terminal("budget:wall_time_s")
        # Keep the running conversation within the model's context window across a
        # long multi-step run (elide old tool observations); off unless configured.
        messages = _bounded_history(messages, max_context_chars)
        try:
            resp = await asyncio.wait_for(
                turn_provider.complete(ModelRequest(messages=messages, tools=tool_schemas)),
                timeout=remaining,
            )
        except TimeoutError:
            return await run.terminal("budget:wall_time_s")
        tokens += _usage_tokens(resp.usage)
        # Record this call's usage, tier, and model into the log so cost is
        # reconstructable after the fact (a read-side projection sums these);
        # ``model`` is whatever the provider exposes (None for the fake one).
        await run.emit(
            ev.TURN_COMPLETED,
            {
                "step": step + 1,
                "tier": ladder.current,
                "model": getattr(turn_provider, "model", None),
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
        reason = _budget_reason(time.monotonic() - start, tokens, budget)
        if reason is not None:
            return await run.terminal(reason)

        # A truncated response is unusable whether it finalised OR called tools:
        # tool-call arguments cut off mid-generation are exactly the malformed
        # untrusted output we must not dispatch. Check before acting on anything.
        if resp.finish == Finish.LENGTH:
            return await run.terminal("model truncated (length)")

        # --- the model chose actions: dispatch tools, then detector checkpoints ---
        if resp.tool_calls:
            if len(resp.tool_calls) > budget.max_tool_calls:
                return await run.terminal("budget:max_tool_calls")
            assistant_msg: dict = {
                "role": "assistant",
                "tool_calls": [{"name": c.name, "args": c.args} for c in resp.tool_calls],
            }
            # Preserve any reasoning text the model emitted alongside its tool
            # calls, so the resent history keeps its train of thought (a model
            # often explains a plan before calling a tool). Omitted when empty so
            # the neutral tool-call shape is unchanged for the text-free case.
            if resp.text:
                assistant_msg["content"] = resp.text
            messages.append(assistant_msg)
            halting: Verdict | None = None
            dispatched = 0
            for call in resp.tool_calls:
                # Bound each tool call by the run's remaining wall-time, so a hung
                # tool cannot overrun the deadline the way a hung provider can't.
                tool_remaining = max(0.0, budget.wall_time_s - (time.monotonic() - start))
                obs = await _invoke(
                    run, turn, active, call.name, call.args, timeout=tool_remaining
                )
                model_obs = await _shrink_for_model(
                    obs, max_chars=max_observation_chars, strategy=observation_strategy,
                    provider=provider, query=spec.query,
                )
                messages.append(
                    {"role": "tool", "name": call.name,
                     "content": json.dumps(model_obs, default=str)}
                )
                dispatched += 1
                halting = await _detector_checkpoint(run, turn, detectors, obs, {Scope.PER_OBSERVATION})
                if halting is not None:
                    break  # stop dispatching this turn's remaining calls; act on it
            # A detector that halted mid-turn left the rest of THIS turn's tool
            # calls un-dispatched. Each still needs a tool-result message, or the
            # resent history has an assistant tool_call with no matching result —
            # malformed for the provider adapters (the loop continues on ESCALATE).
            for skipped in resp.tool_calls[dispatched:]:
                messages.append(
                    {"role": "tool", "name": skipped.name,
                     "content": json.dumps(
                         {"skipped": f"not run: {halting.detector} ({halting.severity.value})"}
                         if halting else {"skipped": "not run"})}
                )
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


async def _invoke(
    run: _Run, turn: UUID, tools: dict, name: str, args: dict, *, timeout: float | None = None
) -> dict:
    """Dispatch one tool call to an observation. A missing tool or a raising
    tool (e.g. an SSRF block) becomes an error observation, never a crash —
    the same isolation principle the bus applies to subscribers. Unexpected
    failures are logged so a real bug isn't silently disguised as data.

    ``tools`` is the *active* set for the current tier, so a call to a tool
    that hasn't been unlocked yet falls into the unknown-tool branch — the
    ladder is enforced on dispatch, not just on what the model is shown.

    ``timeout`` bounds the tool call (the run's remaining wall-time): tools are
    the untrusted/3rd-party surface, and without this a tool hung on a dead
    socket would block forever and defeat ``wall_time_s`` (which is otherwise
    only re-checked between turns). A timeout becomes an error observation — the
    same isolation a raise gets — and the next budget checkpoint ends the run."""
    await run.emit(ev.TOOL_INVOKED, {"tool": name, "args": args}, parent=turn, source=name)
    tool = tools.get(name)
    if tool is None:
        obs: dict = {"error": f"unknown tool: {name}"}
    else:
        try:
            call = tool(run.ctx(), **args)
            obs = await (asyncio.wait_for(call, timeout) if timeout is not None else call)
        except TimeoutError:
            log.warning("tool %r exceeded its %.3fs deadline; rejecting it", name, timeout or 0.0)
            await run.emit(
                ev.DEFENSE_BLOCKED,
                {"kind": "tool_timeout", "tool": name,
                 "detail": "tool call exceeded the run's remaining wall-time and was cancelled"},
                parent=turn,
                source=name,
            )
            obs = {"error": "tool call timed out", "blocked": "tool_timeout"}
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
    run: _Run, turn: UUID, detectors: list, observation: Any, scopes: set[Scope]
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
        verdict = _safe_inspect(d, ctx)
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
            v = _safe_inspect(d, ctx)
            if v is not None:
                verdicts.append(v)
    for val in validators:
        v = _safe_check(val, candidate, ctx)
        if v is not None:
            verdicts.append(v)
    return _worst(verdicts)
