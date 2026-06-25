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
import random
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID, uuid5

from . import events as ev
from .bus import EventBus
from .contracts import Budget, Event, Result, Status, TaskSpec
from .grants import InMemoryGrantStore
from .ledger import InMemoryExecutionLedger
from .ports import (
    Finish,
    ModelProvider,
    ModelRequest,
    MonitorState,
    MonitorVerdict,
    ReplayDecision,
    RunContext,
    Scope,
    Severity,
    ToolCall,
    Verdict,
    declared_envelope,
)
from .registry import REGISTRY, Registry
from .runlifecycle import close_run as _run_cleanup
from .security import SecurityBlock, _needs_containment, enforce_containment
from .track import MAX_REPLAY_WAIT_MS, Track, TrackStep, replay_extra_delay_ms

log = logging.getLogger("zu.loop")

# Severity ordering for picking the worst verdict at a checkpoint.
_RANK = {
    Severity.WARN: 0,
    Severity.RETRY: 1,
    Severity.ESCALATE: 2,
    Severity.DENY: 3,
    Severity.TERMINAL: 4,
}

# The Monitor→Severity bridge (ZU-RAIL-5). The Monitor port speaks a policy-neutral
# vocabulary (OK/WARN/VIOLATION); the runtime owns what that MEANS for the loop:
# a VIOLATION halts the run (TERMINAL), a WARN is recorded and the run continues.
# Kept in the loop (not the port) so the automaton stays a pure property and the
# escalation semantics live with the interpreter that owns them.
_MONITOR_SEVERITY: dict[MonitorState, Severity] = {
    MonitorState.WARN: Severity.WARN,
    MonitorState.VIOLATION: Severity.TERMINAL,
}

# How many consecutive SOFT misses (no-op actions) the replay navigator tolerates
# before deciding the recorded path has truly diverged and handing off to the model.
# One or two are normal drift (an already-dismissed banner); a run of them is not.
_REPLAY_MAX_SOFT_MISSES = 3

# After a CLEAN replay the navigation is done and its observations (the gathered
# evidence — e.g. the available slots) are already in the message history. Without
# this nudge the model, seeing a finished/closed session, sometimes decides it must
# start over and re-drives the whole flow (observed: 8 calls vs 2, blowing the
# budget). This pins it to the cheap branch: extract from history, don't re-navigate.
_REPLAY_DONE_NOTICE = (
    "The recorded navigation above is complete — its tool observations already "
    "contain the evidence gathered for this task. Produce the final answer NOW by "
    "extracting it from those observations. Do NOT re-open the browser, re-run a "
    "search, or repeat any navigation; the session has already served its purpose."
)

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
_FENCE_ANY = re.compile(r"```[a-zA-Z0-9]*[ \t]*\n?(.*?)```", re.DOTALL)


def _balanced_object(text: str) -> str | None:
    """The first balanced ``{...}`` run in ``text`` — so a JSON object the model
    embedded in prose can be recovered. String- and escape-aware, so a brace
    inside a JSON string value never ends the object early."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_value(text: str | None) -> dict | None:
    """Turn the model's final text into a structured value (always a dict — what
    the result contract and schema/grounding validation expect).

    Real models rarely emit a bare JSON object: they wrap it in a markdown code
    fence and routinely PREPEND prose ("Here are the results: ```json {…}```").
    A start-anchored fence misses that, leaving the whole prose treated as opaque
    text — which then fails grounding and burns the whole budget on retries (seen
    live). So try, in order: the whole text as JSON; a single enclosing fence; a
    fenced block anywhere; the first balanced ``{…}`` embedded in prose. Only if
    none parse is the text kept opaque (``{"text": …}``) — the last resort."""
    if not text:
        return None
    stripped = text.strip()
    candidates = [stripped]
    enclosing = _FENCE.match(stripped)
    if enclosing:
        candidates.append(enclosing.group(1).strip())
    anywhere = _FENCE_ANY.search(text)
    if anywhere:
        candidates.append(anywhere.group(1).strip())
    embedded = _balanced_object(text)
    if embedded:
        candidates.append(embedded)
    for cand in candidates:
        try:
            parsed = json.loads(cand)
        except (ValueError, TypeError):
            continue
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"text": text}


def _summarize_observation(obs: dict) -> dict:
    """A compact view of an observation for the harness.tool.returned event:
    drop the large content fields (kept in full in data.source.fetched) and
    replace them with their length, so a fetched page isn't stored twice."""
    summary = {k: v for k, v in obs.items() if k not in _CONTENT_KEYS}
    for k in _CONTENT_KEYS:
        if isinstance(obs.get(k), str):
            summary[f"{k}_len"] = len(obs[k])
    return summary


def _perception_action_events(obs: dict) -> list[tuple[str, dict]]:
    """Map a tool observation's SHAPE to the perception/action data events (§4.5 /
    §5.4) — tool-agnostic, the same way ``_CONTENT_KEYS`` drives data.source.fetched.

    * an ``action_surface`` key → ``data.surface.captured``: the EXACT surface shown
      to the policy (counts + handle list + blind flag), so a reviewer can
      reconstruct what the agent could perceive/do here. The role+name locators stay
      harness-side (never on the log); the handle list is the auditable record.
    * a ``pointer`` key → ``data.pointer.dispatched``: the trajectory summary (the
      full per-sample path rides in the tool observation for replay).
    """
    out: list[tuple[str, dict]] = []
    surface = obs.get("action_surface")
    if isinstance(surface, dict):
        affordances = surface.get("affordances") or []
        out.append((ev.SURFACE_CAPTURED, {
            "url": surface.get("url", ""),
            "title": surface.get("title", ""),
            "affordances": len(affordances) if isinstance(affordances, list) else 0,
            "handles": [a.get("handle") for a in affordances if isinstance(a, dict)]
            if isinstance(affordances, list) else [],
            "context": len(surface.get("context") or []),
            "blind": bool(obs.get("surface_blind", surface.get("blind", False))),
            "blind_reason": surface.get("blind_reason"),
        }))
    pointer = obs.get("pointer")
    if isinstance(pointer, dict):
        out.append((ev.POINTER_DISPATCHED, {
            "handle": pointer.get("handle"),
            "clicked": bool(pointer.get("clicked", False)),
            "samples": pointer.get("samples", 0),
            "duration_ms": pointer.get("duration_ms", 0.0),
            "dest": pointer.get("dest", {}),
            "seed": str(pointer.get("seed", "")),
        }))
    return out


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


def _safe_evaluate(monitor: Any, ctx: RunContext) -> MonitorVerdict | None:
    """Run a Monitor in isolation (ZU-RAIL-5): a raising third-party monitor is
    logged and skipped, never allowed to crash the run — the same isolation
    ``_safe_inspect`` gives detectors. A Monitor is pure, but a buggy one must not
    halt the loop."""
    try:
        verdict: MonitorVerdict | None = monitor.evaluate(ctx)
        return verdict
    except Exception as exc:  # noqa: BLE001 - a broken monitor must not halt the run
        log.warning(
            "monitor %r raised %s: %s — skipping it",
            getattr(monitor, "name", monitor), type(exc).__name__, exc,
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


class _GateEscalation(Exception):
    """Raised inside ``_invoke`` when a pre-execution gate returns an ESCALATE
    verdict (ZU-CORE-2). It carries the literal ``call`` and the ``verdict`` so
    the dispatch site — which owns the ladder, the message list, and (Phase 4)
    the human-pause — decides whether to climb a tier or pause for approval. The
    tool body never ran, so there is no observation to return; this keeps
    ``_invoke``'s return type ``dict`` for the allow/deny cases."""

    def __init__(self, call: ToolCall, verdict: Verdict, idempotency_key: str) -> None:
        self.call = call
        self.verdict = verdict
        self.idempotency_key = idempotency_key
        super().__init__(f"gate {verdict.detector} escalated {call.name}")


def _safe_gate(
    gate: Any, call: ToolCall, ctx: RunContext
) -> tuple[Verdict | None, Exception | None]:
    """Run one InvocationGate in isolation and REPORT the outcome as
    ``(verdict, crash)``. The crash is no longer swallowed into a silent "no
    verdict": the caller decides what a crash means per ZU-CORE-2 — **fail closed**
    (synthesize a DENY) for a capability-bearing / tier-≥2 call, where a crashed
    scope-checker must never be a bypass, and **fail-open-but-logged** for an inert
    tier-1 call, where a broken gate must not break an ordinary web fetch. An
    explicit verdict (incl. DENY) is returned with ``crash=None`` and honoured as
    before. The exception is still logged here; it is not discarded."""
    try:
        verdict: Verdict | None = gate.check(call, ctx)
        return verdict, None
    except Exception as exc:  # noqa: BLE001 - a broken gate must not crash the run
        log.warning(
            "gate %r raised %s: %s",
            getattr(gate, "name", gate), type(exc).__name__, exc,
        )
        return None, exc


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

    def __init__(
        self,
        spec: TaskSpec,
        bus: EventBus,
        trace_id: UUID | None = None,
        *,
        grants: Any = None,
        ledger: Any = None,
    ) -> None:
        self.spec = spec
        self.bus = bus
        # ``trace_id`` correlates a run's events. It defaults to the task id (one
        # trace per task), but a multi-phase pipeline passes a shared id so every
        # phase's events fold into one replayable lineage (see zu.Pipeline). The
        # per-phase ``task_id`` stays distinct, so a phase is still queryable alone.
        self.trace_id = trace_id if trace_id is not None else spec.task_id
        self.task_id = spec.task_id
        # Run-level taint (ZU-CD-3): seeded from the spec (a caller folding hostile
        # trigger input sets ``spec.tainted``); a tool can also flip it mid-run.
        self.tainted: bool = bool(getattr(spec, "tainted", False))
        # Run mode (ZU-RAIL-2): "execute" (default) or "explore". In explore the
        # loop disarms capability-bearing tool calls (stub instead of execute).
        self.mode: str = str(getattr(spec, "mode", "execute") or "execute")
        # Durable per-grant state (ZU-CD-4): the injected store or the in-memory
        # default (a cache over ``harness.grant.updated`` events).
        self.grant_state: Any = grants if grants is not None else InMemoryGrantStore()
        # Consume-once execution ledger (ZU-CD-6): the injected ledger or the
        # in-memory default (a cache over ``harness.execution.claimed`` events). The
        # loop claims against it before re-executing a human-approved invocation on
        # resume, so a double-resume cannot double-execute an irreversible side effect.
        self.exec_ledger: Any = ledger if ledger is not None else InMemoryExecutionLedger()
        # One RunContext reused for the whole run: ``observation`` is updated
        # per checkpoint — so a checkpoint is O(1), not an O(n) copy of the log.
        # Plugins (tools/detectors/validators) receive ``ctx.events`` as a
        # *read-only window* onto the live log: it reflects the log as it grows
        # (no copy) but a misbehaving plugin cannot mutate or corrupt the
        # canonical record through it. The loop appends to ``self.events``.
        self.events: list[Event] = []
        # Monotonic per-run dispatch counter — the deterministic basis for the
        # idempotency key (ZU-CORE-4). It depends only on call position, not on a
        # random event_id, so a replay of the same trace mints the same keys.
        self._call_seq = 0
        self._ctx = RunContext(spec=spec, observation=None, events=[])
        self._ctx.events = _EventsView(self.events)
        self._ctx.grants = self.grant_state
        self._ctx.execution = self.exec_ledger
        self._ctx.tainted = self.tainted
        self._ctx.mode = self.mode
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

    def ctx(
        self, observation: Any = None, *, invocation: Any = None,
        idempotency_key: str | None = None, annotations: dict | None = None,
    ) -> RunContext:
        # Reuse the single context object; just point it at the current
        # observation. Detectors/validators read it as a read-only view. The gate
        # receives the pending ``invocation`` and the rail step's ``annotations``
        # (ZU-RAIL-4); a tool receives its ``idempotency_key``. All reset to None
        # outside their checkpoint so they never leak across calls; ``tainted`` and
        # ``mode`` are refreshed so a mid-run flip is visible at the gate.
        self._ctx.observation = observation
        self._ctx.invocation = invocation
        self._ctx.idempotency_key = idempotency_key
        self._ctx.annotations = annotations
        self._ctx.tainted = self.tainted
        self._ctx.mode = self.mode
        return self._ctx

    def raise_taint(self, source: str, detail: str | None = None) -> bool:
        """Flip the run-level taint flag on (ZU-CD-3). Returns True if it changed
        false->true (the caller then emits ``harness.taint.raised``)."""
        if self.tainted:
            return False
        self.tainted = True
        self._ctx.tainted = True
        return True

    async def flush_grants(self, parent: UUID | None = None) -> None:
        """Drain the in-memory grant store's journal and record each write as a
        ``harness.grant.updated`` event (ZU-CD-4), so a paused run rebuilds its
        cumulative counters from the log on resume. A durable/plugin store with no
        journal is a no-op (it persists itself)."""
        drain = getattr(self.grant_state, "drain", None)
        if drain is None:
            return
        for grant_id, key, value in drain():
            await self.emit(
                ev.GRANT_UPDATED,
                {"grant_id": grant_id, "key": key, "value": value},
                parent=parent or self.root,
            )

    async def flush_claims(self, parent: UUID | None = None) -> None:
        """Drain the in-memory execution ledger's journal and record each claim as a
        ``harness.execution.claimed`` event (ZU-CD-6), so a resumed/replayed run
        rebuilds its claimed set and refuses to re-execute a claimed side effect. A
        durable/plugin ledger with no journal is a no-op (it persists itself)."""
        drain = getattr(self.exec_ledger, "drain", None)
        if drain is None:
            return
        for key in drain():
            await self.emit(
                ev.EXECUTION_CLAIMED, {"key": key}, parent=parent or self.root
            )

    async def mark_checkpoint(self, label: str) -> UUID:
        """Mark a last-known-good (LKG) rollback point (ZU-RAIL-8). Emits
        ``harness.checkpoint.marked`` {"label", "step"} parented to run.root; the
        ``step`` is the current log length, so ``last_known_good`` can locate the
        marker. Returns the marker event's id (the restore target)."""
        return await self.emit(
            ev.CHECKPOINT_MARKED, {"label": label, "step": len(self.events)}, parent=self.root
        )

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
    """Did a replayed step hit a HARD challenge — i.e. diverge from the recorded path
    so the model must take over? A tool error, a blocked/refused call, or an HTTP
    error status. (A successful step that merely returns different live DATA is NOT a
    challenge — the navigation still worked.)

    A SOFT action miss (``action_error_kind == 'soft'``: an element-targeting action
    that found no target) is NOT a hard challenge: the element is often gone because
    its goal already holds — a consent banner already dismissed, an option already
    selected — so it is a no-op, not a broken page. The replay keeps going; only a
    RUN of consecutive soft misses (handled by the navigator) signals real divergence."""
    if not isinstance(obs, dict):
        return False
    if obs.get("error") or obs.get("blocked"):
        return True
    if obs.get("action_error") and obs.get("action_error_kind") != "soft":
        return True
    status = obs.get("status")
    return isinstance(status, int) and status >= 400


def _is_soft_miss(obs: Any) -> bool:
    """A replayed step that no-op'd: an element-targeting action missed its target on
    an otherwise-healthy page. Tolerated singly, but a run of them means divergence."""
    return isinstance(obs, dict) and obs.get("action_error_kind") == "soft"


async def _replay_climb_to(run: _Run, ladder: _Ladder, tools: dict, step: Any) -> None:
    """Climb the ladder to the tier a replayed step needs — the larger of the
    recorded tier and the tool's own tier — emitting the ``task.escalated`` event
    the model's climb would, so the replay reproduces (and re-records) escalation.
    Capped at the ceiling; a no-op when already high enough."""
    tool = tools.get(step.tool)
    want = max(getattr(step, "tier", 1), _tier_of(tool) if tool is not None else 1)
    want = min(want, ladder.ceiling)
    if want <= ladder.current:
        return
    frm = ladder.current
    ladder.current = want
    await run.emit(
        ev.TASK_ESCALATED,
        {"reason": "replay", "from_tier": frm, "to_tier": want, "replay": True},
        parent=run.root, source="replay",
    )


_REPLAY_DECISION_RANK = {
    ReplayDecision.CONTINUE: 0,
    ReplayDecision.HANDOFF: 1,
    ReplayDecision.ESCALATE: 2,
    ReplayDecision.STOP: 3,
}


def _step_annotations(step: TrackStep) -> dict | None:
    """The rail step's blessed annotations (ZU-RAIL-4) as a ctx dict, or None."""
    ann: dict = {}
    if step.consequence is not None:
        ann["consequence"] = step.consequence
    if step.destination is not None:
        ann["destination"] = step.destination
    return ann or None


def _arbitrate(arbiters: list | tuple, step: TrackStep, observation: Any, ctx: RunContext) -> ReplayDecision:
    """Ask every ReplayArbiter and take the *strongest* decision (STOP > ESCALATE >
    HANDOFF > CONTINUE). A raising arbiter is isolated (logged, treated as CONTINUE)
    so a buggy arbiter cannot crash a replay — its job is to *raise* the bar, never
    to be load-bearing for safety on its own."""
    worst = ReplayDecision.CONTINUE
    for a in arbiters:
        try:
            d = a.decide(step, observation, ctx)
        except Exception as exc:  # noqa: BLE001 - a broken arbiter must not crash replay
            log.warning("replay arbiter %r raised %s: %s", getattr(a, "name", a),
                        type(exc).__name__, exc)
            continue
        if _REPLAY_DECISION_RANK.get(d, 0) > _REPLAY_DECISION_RANK[worst]:
            worst = d
    return worst


async def _replay_track(
    run: _Run, track: Track, tools: dict, messages: list[dict], ladder: _Ladder, *,
    wall_time_s: float, start: float, max_observation_chars: int | None,
    jitter_median_ms: int = 0, gates: list | tuple = (),
    arbiters: list | tuple = (), tokens: int = 0,
) -> tuple[bool, Result | None]:
    """Drive the recorded path deterministically — re-issue each tool call in order,
    with NO model call — appending the same assistant/tool message pair the loop
    would, so the model has consistent history if it takes over. Returns
    ``(diverged, result)``: ``diverged=True`` hands the frontier to the model;
    ``result`` is set (and returned by ``run_task`` immediately) when a
    ``ReplayArbiter`` paused for a human or stopped the run. Paced by the recorded
    gaps (capped), and bounded by the run's wall-time.

    **The replay-divergence arbiter (ZU-RAIL-3):** before issuing each step, every
    registered arbiter is shown the recorded ``step`` and the *prior* step's live
    observation (the page state the step is about to act on) and returns
    CONTINUE / HANDOFF / ESCALATE / STOP. ESCALATE pauses for a HUMAN (reusing the
    ZU-CD-1/2/5 pause/resume — the step becomes the pending, human-approved
    invocation), STOP ends the run, HANDOFF gives the frontier to the model (Zu's
    existing default), CONTINUE proceeds on rails. With **no** arbiter registered,
    the existing challenge/soft-miss → hand-to-model behaviour below is unchanged.

    ``jitter_median_ms`` humanises the pacing of a live run: the recorded gap is the
    absolute floor and each step adds a stationary, heavy-tailed (log-normal) extra
    — most steps a little, the occasional one a second or two (or longer) — so a
    driven path is not fired at a uniform machine cadence and does not creep upward
    as the run goes on. Seeded from the run's ``trace_id`` (reproducible per run);
    0 by default, and when off the recorded gap is capped so offline iteration and
    tests stay instant.

    The track remembers its escalation: before a step that needs a higher tier (its
    recorded tier, or the tool's own tier), the navigator climbs the ladder and emits
    the same ``task.escalated`` event the model's climb would — so the replay is a
    faithful re-run (and a re-recording captures the escalation), and the model
    inherits the ladder at the tier the path had reached.

    A single SOFT miss (a no-op action — clicking an already-dismissed banner) does
    NOT end replay: the path is still on track. Only a RUN of consecutive soft misses
    (``_REPLAY_MAX_SOFT_MISSES``) means the page really diverged — then hand off."""
    soft_streak = 0
    last_obs: Any = None  # the prior step's observation — the page the next step acts on
    # Seeded from the run's trace_id so the humanised pacing is reproducible for a
    # given run (and a fixed trace_id in tests), varied across runs.
    jitter_rng = random.Random(str(run.trace_id))
    for i, step in enumerate(track.steps):
        if wall_time_s - (time.monotonic() - start) <= 0:
            return True, None  # out of time; let the model loop end the run cleanly
        await _replay_climb_to(run, ladder, tools, step)
        # Replay-divergence arbitration (ZU-RAIL-3): consult BEFORE issuing the
        # step, on the prior observation, so an ESCALATE pauses for a human BEFORE a
        # consequential action runs (and resume executes that exact approved step).
        annotations = _step_annotations(step)
        if arbiters:
            decision = _arbitrate(arbiters, step, last_obs, run.ctx(observation=last_obs, annotations=annotations))
            if decision is ReplayDecision.STOP:
                return False, await run.terminal("replay.arbiter.stop")
            if decision is ReplayDecision.ESCALATE:
                idem = str(uuid5(
                    run.trace_id, f"replay:{i}:{step.tool}:{json.dumps(step.args, sort_keys=True, default=str)}"))
                verdict = Verdict(severity=Severity.ESCALATE, detector="replay_arbiter",
                                  detail="consequential replay divergence", kind="human")
                result = await _pause_for_human(
                    run, ladder, tokens, i, ToolCall(name=step.tool, args=step.args), verdict, idem,
                    annotations=annotations)
                return False, result
            if decision is ReplayDecision.HANDOFF:
                return True, None  # arbiter hands the frontier to the model
            # CONTINUE: proceed on rails.
        if jitter_median_ms > 0:
            # Live run: the recorded gap is the absolute FLOOR (honoured in full),
            # plus a stationary, heavy-tailed extra — most steps a little, the
            # occasional one a second or two (or longer). Humanised pacing.
            wait_ms = step.wait_ms + replay_extra_delay_ms(jitter_rng, median_ms=jitter_median_ms)
        else:
            # Offline / iteration / tests: cap the recorded gap so replay stays
            # fast and there is no added jitter.
            wait_ms = min(step.wait_ms, MAX_REPLAY_WAIT_MS)
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        turn = await run.emit(ev.TURN_STARTED, {"step": i + 1, "replay": True}, parent=run.root)
        remaining = max(0.0, wall_time_s - (time.monotonic() - start))
        try:
            obs = await _invoke(run, turn, tools, step.tool, step.args,
                                gates=gates, timeout=remaining, annotations=annotations)
        except _GateEscalation as esc:
            # A gate intervened on a replayed step (ZU-CORE-2): the recorded path
            # is no longer free to proceed unattended — hand the frontier to the
            # live model, which re-encounters the gate under full control.
            messages.append(
                {"role": "assistant", "content": f"(replay step {i + 1})",
                 "tool_calls": [{"name": step.tool, "args": step.args}]}
            )
            messages.append(
                {"role": "tool", "name": step.tool,
                 "content": json.dumps({"escalated": esc.verdict.detail or esc.verdict.detector})}
            )
            return True, None
        last_obs = obs
        messages.append(
            {"role": "assistant", "content": f"(replay step {i + 1})",
             "tool_calls": [{"name": step.tool, "args": step.args}]}
        )
        messages.append(
            {"role": "tool", "name": step.tool,
             "content": json.dumps(_observation_for_model(obs, max_observation_chars), default=str)}
        )
        if _is_challenge(obs):
            return True, None  # diverged — hand the frontier to the model from here
        if _is_soft_miss(obs):
            soft_streak += 1
            if soft_streak >= _REPLAY_MAX_SOFT_MISSES:
                return True, None  # too many no-ops in a row — the path really diverged
        else:
            soft_streak = 0
    return False, None


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
    replay_budget: Budget | None = None,
    finish_provider: ModelProvider | None = None,
    replay_jitter_median_ms: int = 0,
    grants: Any = None,
    ledger: Any = None,
    resume_from: Sequence[Event] | None = None,
    approved_rail_hash: str | None = None,
    _rollback: Any = None,
) -> Result:
    """Drive one task to a Result, then fire run-end cleanup at TRUE run end.

    A thin wrapper over :func:`_run_task` whose only job is the run-end lifecycle:
    in a ``finally`` it invokes the registered run-cleanup hooks
    (:func:`zu_core.runlifecycle.close_run`) so a plugin's run-scoped resources (e.g.
    a shared browser container) are released exactly once when the run ENDS —
    terminal/escalate/success, or a crash — but NOT on a human pause, which suspends
    the run for resume and must keep its run-scoped state alive. The cleanup contract
    is one generic string (the run key, ``str(spec.task_id)``) — never a live handle —
    so zu-core stays SDK-free and the seam is inert until a plugin registers a hook."""
    result: Result | None = None
    try:
        result = await _run_task(
            spec, provider, registry, bus,
            providers=providers, containment=containment, trace_id=trace_id,
            max_observation_chars=max_observation_chars,
            observation_strategy=observation_strategy, max_context_chars=max_context_chars,
            track=track, replay_budget=replay_budget, finish_provider=finish_provider,
            replay_jitter_median_ms=replay_jitter_median_ms, grants=grants, ledger=ledger,
            resume_from=resume_from, approved_rail_hash=approved_rail_hash, _rollback=_rollback,
        )
        return result
    finally:
        # A human pause SUSPENDS the run (it resumes later) — keep its run-scoped
        # state. Any other outcome (incl. an exception, where ``result`` is None)
        # is a true run end: release run-scoped resources exactly once.
        if result is None or result.status is not Status.PAUSED:
            await _run_cleanup(str(spec.task_id))


async def _run_task(
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
    replay_budget: Budget | None = None,
    finish_provider: ModelProvider | None = None,
    replay_jitter_median_ms: int = 0,
    grants: Any = None,
    ledger: Any = None,
    resume_from: Sequence[Event] | None = None,
    approved_rail_hash: str | None = None,
    _rollback: Any = None,
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

    ``replay_budget`` and ``finish_provider`` make replay cheap at maturity. When a
    matching ``track`` replays, ``replay_budget`` (if given) REPLACES the task budget
    for that run — tight, because the navigation is solved, so a broken track fails
    fast and cheap instead of silently re-pathfinding at full cost. And if replay
    finishes WITHOUT diverging, ``finish_provider`` (a cheap model) drives the
    frontier — typically just the final extraction; on a divergence the strong
    ``provider`` stays in charge to re-pathfind. Both are no-ops on a non-replay run.

    ``replay_jitter_median_ms`` humanises a replayed track's pacing: the recorded
    gap is the absolute floor and each step adds a stationary, heavy-tailed
    (log-normal) extra with this median — most steps a little, the occasional one a
    second or two (or longer), and it does NOT creep upward as the run goes on.
    Seeded from the run's trace_id so it is reproducible. It is 0 (off) by default —
    live runs turn it on; offline replay and tests leave it off so iteration stays
    instant.

    ``containment`` is the fail-closed floor (see ``zu_core.security``): with
    ``"required"``, a tool with off-box reach is refused unless the run is inside
    the Zu sandbox; ``"audit"`` (default) runs in-process and logs declarations.
    The check runs *before* any tool is built or dispatched, so an uncontained
    capability tool never executes even once.
    """
    by_tier: Mapping[int, ModelProvider] = providers or {}
    registry = registry if registry is not None else REGISTRY
    bus = bus or EventBus()
    run = _Run(spec, bus, trace_id=trace_id, grants=grants, ledger=ledger)
    # At maturity a matching track makes the run a deterministic replay: apply the
    # tight replay budget (a broken track then fails fast, not at full pathfinding
    # cost). The pathfinding budget still governs a fresh/--no-track run.
    replaying = track is not None and track.matches(spec.query) and bool(track.steps)
    budget = replay_budget if (replaying and replay_budget is not None) else spec.budget

    tools = {name: _materialize(registry.get("tools", name)) for name in registry.names("tools")}
    detectors = [_materialize(registry.get("detectors", n)) for n in registry.names("detectors")]
    validators = [_materialize(registry.get("validators", n)) for n in registry.names("validators")]
    # The pre-execution gate set (ZU-CORE-2). Empty by default, so a run with no
    # registered gate behaves exactly as before — the seam is inert until used.
    gates = [_materialize(registry.get("gates", n)) for n in registry.names("gates")]
    # The replay-divergence arbiters (ZU-RAIL-3). Empty by default ⇒ the navigator's
    # existing challenge/soft-miss → hand-to-model behaviour is unchanged.
    arbiters = [_materialize(registry.get("replay_arbiters", n))
                for n in registry.names("replay_arbiters")]
    # The stateful, history-aware monitors (ZU-RAIL-5). Empty by default ⇒ the
    # monitor checkpoint short-circuits and the event sequence is unchanged.
    monitors = [_materialize(registry.get("monitors", n)) for n in registry.names("monitors")]

    # Fail-closed containment floor: refuse before anything runs if a tool needs a
    # sandbox we're not inside. Raised (not a Result) — a misconfigured posture is
    # an operator error, surfaced loudly like a bad config, not a task outcome.
    enforce_containment(containment, tools)

    # The tier ladder gates which tools the model sees; the run starts at tier 1.
    ladder = _Ladder(tools, spec.max_tier)

    start = time.monotonic()
    tokens = 0

    if _rollback is not None:
        # --- restore-to-last-known-good rollback (ZU-RAIL-8) -----------------
        # Re-seat the spine from the GOOD PREFIX of a prior log (dropping the failed
        # tail), then RE-ENTER THE MODEL LOOP from a fresh turn so the model picks a
        # DIFFERENT path — distinct from the forward-resume branch below, which
        # executes the one pending approved invocation and moves forward past a pause.
        tokens = await _seed_from_rollback(run, ladder, _rollback)
        messages = _initial_messages(spec, ladder.active().values())
    elif resume_from is None:
        run.root = await run.emit(
            ev.TASK_STARTED,
            {"query": spec.query, "target": spec.target, "tainted": run.tainted, "mode": run.mode},
        )

        # Record each tool's declared capability envelope onto the log at run
        # start, so the out-of-band verdict observers (the gate, and the always-on
        # runtime checks) can judge observed behaviour against what each plugin
        # declared.
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
    else:
        # --- resume a paused run from its log (ZU-CD-5) -----------------------
        # Rebuild the run's security spine — tier, tokens, taint, durable grant
        # counters, the dispatch counter — from the prior events, so the resumed
        # run stays bounded by the same gate, taint, and limits. Then resolve the
        # pending human approval and execute ONLY that exact invocation.
        paused, tokens, messages = await _resume_from_log(
            run, resume_from, ladder, gates, spec,
            max_observation_chars=max_observation_chars,
            observation_strategy=observation_strategy, provider=provider,
        )
        if paused is not None:
            return paused  # still awaiting a human resolution -> stay paused

    # --- replay a recorded track first (the navigator): drive the model's known
    # path deterministically, with NO model calls, until a step challenges (errors)
    # or the track runs out. The model loop below then takes over at that frontier,
    # sharing the same tools (and live browser session) and message history.
    finishing = False
    if replaying and resume_from is None:
        assert track is not None  # `replaying` already established this
        # Rail integrity (ZU-RAIL-1): if the caller pinned an approved content hash,
        # verify the track being replayed IS that exact human-approved rail BEFORE
        # any step runs. A mismatch refuses to replay (an unapproved/tampered rail
        # is never run); a match is recorded. The signature/scope behind the
        # approval is the consumer's policy (ride it in payload["ctx"]).
        if approved_rail_hash is not None:
            actual = track.content_hash()
            if actual != approved_rail_hash:
                await run.emit(
                    ev.DEFENSE_BLOCKED,
                    {"kind": "rail_unapproved", "detail": "track content hash does not match the "
                     "approved rail", "expected": approved_rail_hash, "actual": actual},
                    parent=run.root,
                )
                return await run.terminal("rail.unapproved")
            await run.emit(ev.RAIL_VERIFIED, {"rail_hash": actual}, parent=run.root)
        # The navigator climbs the ladder as the recorded path did (emitting the
        # same escalation events), so when the model takes over at the frontier it
        # inherits the ladder exactly where the path left it — its remembered tier,
        # not a blanket jump to the ceiling.
        diverged, replay_result = await _replay_track(
            run, track, tools, messages, ladder,
            wall_time_s=budget.wall_time_s, start=start,
            max_observation_chars=max_observation_chars,
            jitter_median_ms=replay_jitter_median_ms, gates=gates,
            arbiters=arbiters, tokens=tokens,
        )
        # An arbiter (ZU-RAIL-3) may have paused for a human or stopped the run —
        # that Result is the run's outcome, returned immediately.
        if replay_result is not None:
            return replay_result
        # Replay reached the frontier cleanly (no challenge): what's left is usually
        # just the final extraction, so a cheap finish_provider can close it out. A
        # divergence means real re-pathfinding — keep the strong provider for that.
        finishing = not diverged and finish_provider is not None
        if not diverged:
            # Pin the model to "extract from history" instead of re-navigating.
            messages.append({"role": "user", "content": _REPLAY_DONE_NOTICE})

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
        # A clean-replay finish uses the cheap finisher for the whole frontier;
        # otherwise the per-tier override (or the global provider) drives the turn.
        turn_provider = by_tier.get(ladder.current, provider)
        if finishing and finish_provider is not None:
            turn_provider = finish_provider

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
                try:
                    obs = await _invoke(
                        run, turn, active, call.name, call.args,
                        gates=gates, timeout=tool_remaining,
                    )
                except _GateEscalation as esc:
                    # A gate escalated this specific call (ZU-CORE-2): the tool did
                    # not run. ``kind="human"`` pauses the run for approval of the
                    # exact invocation (ZU-CD-1/2); any other escalation routes to
                    # the tier ladder via the halting block below (the skipped-calls
                    # loop appends this call's tool-result message).
                    if esc.verdict.kind == "human":
                        return await _pause_for_human(
                            run, ladder, tokens, step, esc.call, esc.verdict, esc.idempotency_key
                        )
                    halting = esc.verdict
                    break
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
                if halting is None:
                    # The history-aware monitors fold the WHOLE log so far (ZU-RAIL-5),
                    # right beside the per-observation detector checkpoint; a VIOLATION
                    # joins the SAME halting handling below as a TERMINAL Verdict.
                    halting = await _monitor_checkpoint(run, turn, monitors, obs)
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
            if halting is None:
                # A per-turn monitor pass: even a turn with no tool calls (or one whose
                # per-observation passes were clean) is folded once, so a temporal
                # property over the turn boundary is checked.
                halting = await _monitor_checkpoint(run, turn, monitors, None)
            if halting is not None:
                if halting.severity == Severity.TERMINAL:
                    return await run.terminal(halting.detector)
                # A detector/monitor may route to a PERSON, not the tier ladder
                # (ZU-CD-1): ``kind="human"`` pauses the run for human handoff on
                # the invocation that produced this observation (the last dispatched
                # call — e.g. the fetch/render that hit a captcha or a declared
                # human-only step). Re-uses ``_pause_for_human`` and every resume/
                # consume-once guarantee unchanged; the idem is minted exactly as
                # ``_invoke`` minted it for that call, so resume binds to it.
                if halting.kind == "human":
                    if resp.tool_calls and dispatched:
                        paused_call = resp.tool_calls[dispatched - 1]
                        idem = str(uuid5(
                            run.trace_id,
                            f"{run._call_seq}:{paused_call.name}:"
                            f"{json.dumps(paused_call.args, sort_keys=True, default=str)}",
                        ))
                        return await _pause_for_human(
                            run, ladder, tokens, step,
                            ToolCall(name=paused_call.name, args=paused_call.args), halting, idem,
                        )
                    # A human verdict with no invocation to bind the approval to (a
                    # per-turn detector or a monitor, where nothing was dispatched this
                    # turn): a human gate that does not gate is worse than a stop. NEVER
                    # silently downgrade to a tier climb — halt loudly for human attention.
                    return await run.terminal(
                        f"human gate ({halting.detector}) fired with no invocation to bind"
                    )
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
    run: _Run,
    turn: UUID,
    tools: dict,
    name: str,
    args: dict,
    *,
    gates: list | tuple = (),
    timeout: float | None = None,
    approved_key: str | None = None,
    annotations: dict | None = None,
) -> dict:
    """Dispatch one tool call to an observation. A missing tool or a raising
    tool (e.g. an SSRF block) becomes an error observation, never a crash —
    the same isolation principle the bus applies to subscribers. Unexpected
    failures are logged so a real bug isn't silently disguised as data.

    ``tools`` is the *active* set for the current tier, so a call to a tool
    that hasn't been unlocked yet falls into the unknown-tool branch — the
    ladder is enforced on dispatch, not just on what the model is shown.

    ``gates`` is the registered ``InvocationGate`` set (ZU-CORE-2): every gate
    runs HERE, before the tool body, against the literal call. A DENY blocks the
    call (the tool never executes) and returns an error observation; an ESCALATE
    raises ``_GateEscalation`` for the dispatch site to climb a tier or pause for
    a human. ``approved_key`` is the idempotency key of a human-approved call on
    resume (ZU-CD-5): the matching call skips the gate (the human already
    approved it) but still records that it was approved.

    ``timeout`` bounds the tool call (the run's remaining wall-time): tools are
    the untrusted/3rd-party surface, and without this a tool hung on a dead
    socket would block forever and defeat ``wall_time_s`` (which is otherwise
    only re-checked between turns). A timeout becomes an error observation — the
    same isolation a raise gets — and the next budget checkpoint ends the run."""
    # Idempotency key (ZU-CORE-4). A human-approved resume passes the EXACT key the
    # approval was bound to (``approved_key``) so the executed call is provably the
    # one approved; otherwise mint it deterministically over (trace, call-position,
    # tool, args) — the per-run dispatch counter, NOT the random turn event_id, so
    # a replay of the same trace mints the same key.
    if approved_key is not None:
        idem = approved_key
    else:
        run._call_seq += 1
        idem = str(
            uuid5(run.trace_id, f"{run._call_seq}:{name}:{json.dumps(args, sort_keys=True, default=str)}")
        )
    invoked_payload: dict = {"tool": name, "args": args, "idempotency_key": idem}
    if annotations:
        # Carry the rail step's blessed annotations (ZU-RAIL-4) under the
        # consumer-field convention (payload["ctx"], ZU-AUDIT-3) so they round-trip
        # capture→replay and are queryable/replayable.
        invoked_payload["ctx"] = dict(annotations)
    invoked_id = await run.emit(ev.TOOL_INVOKED, invoked_payload, parent=turn, source=name)
    call = ToolCall(name=name, args=args)
    if approved_key is not None:
        # Consume-once (ZU-CD-6): a human approval authorises EXACTLY ONE
        # irreversible side effect. Claim the approved key BEFORE executing — a
        # replay or a second resume of the same resolved approval (e.g. a fresh
        # runner re-reading the log) finds it already claimed and is refused, so it
        # cannot double-execute. The claim journals to the log (flush below) so the
        # guarantee survives across instances/processes, not just this object.
        claimer = getattr(run.exec_ledger, "claim", None)
        if claimer is not None and not claimer(idem):
            await run.emit(
                ev.DEFENSE_BLOCKED,
                {"kind": "duplicate_execution", "tool": name,
                 "detail": "approved invocation already executed (consume-once, ZU-CD-6)"},
                parent=invoked_id,
                source="human",
            )
            obs: dict = {"error": "approved invocation already executed",
                         "blocked": "duplicate_execution"}
            await run.emit(ev.TOOL_RETURNED, {"tool": name, "observation": obs}, parent=turn, source=name)
            return obs
        await run.flush_claims(parent=invoked_id)
        # Resumed after a human approval (ZU-CD-5): the gate is satisfied by the
        # recorded resolution bound to this exact key; record that and execute.
        await run.emit(
            ev.GATE_DECIDED,
            {"action_ref": str(invoked_id), "tool": name, "decision": "approved_by_human", "gate": "human"},
            parent=invoked_id,
            source="human",
        )
    elif gates:
        # "Capability-bearing" for the fail-closed decision is the same predicate
        # the containment floor uses (declares any capability/egress, or tier ≥ 2)
        # — read from the target tool already in scope here, so a crashed gate on
        # such a call fails closed (ZU-CORE-2). Unknown tool ⇒ not capability-bearing
        # (it won't execute anyway).
        gate_target = tools.get(name)
        fail_closed = gate_target is not None and _needs_containment(gate_target)
        worst = await _gate_checkpoint(
            run, gates, call, invoked_id=invoked_id, turn=turn,
            fail_closed=fail_closed, annotations=annotations,
        )
        if worst is not None and worst.severity is Severity.DENY:
            await run.emit(
                ev.DEFENSE_BLOCKED,
                {"kind": "gate_denied", "tool": name, "gate": worst.detector, "detail": worst.detail},
                parent=invoked_id,
                source=worst.detector,
            )
            obs = {
                "error": f"blocked by gate {worst.detector}: {worst.detail or 'denied'}",
                "blocked": "gate_denied",
            }
            await run.emit(ev.TOOL_RETURNED, {"tool": name, "observation": obs}, parent=turn, source=name)
            return obs
        if worst is not None and worst.severity is Severity.ESCALATE:
            raise _GateEscalation(call, worst, idem)
    tool = tools.get(name)
    # Explore-mode disarm (ZU-RAIL-2): a capability-bearing / tier-≥2 tool is NOT
    # executed during pathfinding — return a stub so a model loose on a hostile
    # surface is never armed with a live instrument. Same predicate as the
    # containment floor and the fail-closed gate. Inert tier-1 tools run normally.
    if tool is not None and run.mode == "explore" and _needs_containment(tool):
        await run.emit(ev.RAIL_DISARMED, {"tool": name}, parent=invoked_id, source=name)
        obs = {"stubbed": True, "explore": True, "tool": name,
               "detail": "capability-bearing call disarmed in explore mode (ZU-RAIL-2)"}
        await run.emit(ev.TOOL_RETURNED, {"tool": name, "observation": obs}, parent=turn, source=name)
        return obs
    if tool is None:
        obs = {"error": f"unknown tool: {name}"}
    else:
        try:
            coro = tool(run.ctx(idempotency_key=idem, annotations=annotations), **args)
            obs = await (asyncio.wait_for(coro, timeout) if timeout is not None else coro)
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
    # Run-level taint (ZU-CD-3): a tool flags hostile content by returning a
    # truthy ``_taint`` key. Flip the run flag (mechanical, not a policy
    # self-report) and record it; pop the key so it never leaks into the model's
    # observation or the stored content. The check is on shape, not tool name.
    if isinstance(obs, dict) and obs.pop("_taint", False):
        if run.raise_taint(name):
            await run.emit(
                ev.TAINT_RAISED,
                {"source": name, "detail": "tool flagged hostile content"},
                parent=turn,
                source=name,
            )
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
    # Record perception/action on the audit log (§4.5 / §5.4): the action surface
    # the policy was shown, and each pointer trajectory it produced. Keyed on the
    # observation's SHAPE (an ``action_surface``/``pointer`` key), so the loop stays
    # tool-agnostic — exactly like data.source.fetched above.
    if isinstance(obs, dict):
        for etype, payload in _perception_action_events(obs):
            await run.emit(etype, payload, parent=turn, source=name)
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


async def _monitor_checkpoint(
    run: _Run, turn: UUID, monitors: list, observation: Any
) -> Verdict | None:
    """Fold every registered Monitor over the run's event history (ZU-RAIL-5),
    emit ``harness.monitor.fired`` for each non-OK verdict, and return the worst
    halting Verdict for the caller to act on — a VIOLATION maps (via
    ``_MONITOR_SEVERITY``) to a TERMINAL Verdict routed through the SAME halting
    path detectors use; a WARN is recorded and the run continues.

    Short-circuits on an empty monitor list BEFORE touching ``run.ctx`` or the
    log, so a run with no registered monitor is byte-identical to the baseline —
    the seam is inert until used, exactly like gates/arbiters.
    """
    if not monitors:
        return None
    ctx = run.ctx(observation)
    verdicts: list[Verdict] = []
    for m in monitors:
        mv = _safe_evaluate(m, ctx)
        if mv is None or mv.state == MonitorState.OK:
            continue
        await run.emit(
            ev.MONITOR_FIRED,
            {"monitor": mv.monitor, "state": mv.state.value, "detail": mv.detail, "step": mv.step},
            parent=turn,
            source=mv.monitor,
        )
        severity = _MONITOR_SEVERITY[mv.state]
        verdicts.append(Verdict(severity=severity, detector=mv.monitor, detail=mv.detail))
    worst = _worst(verdicts)
    if worst is not None and worst.severity in (Severity.ESCALATE, Severity.TERMINAL):
        return worst
    return None


async def _gate_checkpoint(
    run: _Run, gates: list | tuple, call: ToolCall, *, invoked_id: UUID, turn: UUID,
    fail_closed: bool, annotations: dict | None = None,
) -> Verdict | None:
    """Run every InvocationGate against the pending call BEFORE it executes
    (ZU-CORE-2), emit a ``harness.gate.decided`` per verdict (parented to the
    tool.invoked event so replay can reconstruct which rule decided each action —
    ZU-AUDIT-2), drain any durable-state writes the gates made (ZU-CD-4), and
    return the worst verdict for ``_invoke`` to act on. Allow is the inert
    default (no verdict).

    ``fail_closed`` is set by the caller from the target tool's capability
    envelope (declares any capability/egress, or tier ≥ 2). When a gate *crashes*
    judging such a call, a crashed scope-checker must not be a bypass: synthesize a
    DENY (rule ``gate.crashed.fail_closed``). For an inert tier-1 call the crash is
    tolerated so a broken gate cannot break an ordinary fetch — but never silently:
    a ``gate.crashed.skipped`` decision is recorded either way.

    Note the implicit coupling: the target-tier fail-closed guarantee holds only
    if the gated tool DECLARES its capability envelope (a side-effecting tool
    authored as tier-1 / no-capabilities would have its crashed gate skipped, i.e.
    fail OPEN). A gate that knows it guards something dangerous (money, a card)
    should not depend on the target's self-declaration: it can set
    ``fail_closed_on_crash = True`` on itself to force fail-closed on crash
    REGARDLESS of the target's tier — applied per-gate below.

    ``annotations`` are the rail step's blessed consequence/destination
    (ZU-RAIL-4), surfaced on the ctx so a gate can gate by consequence."""
    ctx = run.ctx(invocation=call, annotations=annotations)
    verdicts: list[Verdict] = []
    for g in gates:
        verdict, crash = _safe_gate(g, call, ctx)
        if crash is not None:
            gname = getattr(g, "name", "gate")
            detail = f"gate crashed ({type(crash).__name__}: {crash})"
            # A gate may force fail-closed on its own crash regardless of the
            # target tool's self-declared tier (ZU-CORE-2): a gate guarding money
            # shouldn't trust the tool to declare itself capability-bearing.
            gate_fail_closed = fail_closed or bool(
                getattr(g, "fail_closed_on_crash", False)
            )
            if gate_fail_closed:
                # Capability-bearing / tier-≥2: fail CLOSED — the crashed gate
                # becomes a DENY so the call is blocked, not bypassed (ZU-CORE-2).
                await run.emit(
                    ev.GATE_DECIDED,
                    {"action_ref": str(invoked_id), "tool": call.name, "decision": "deny",
                     "gate": gname, "rule_id": "gate.crashed.fail_closed", "detail": detail},
                    parent=invoked_id, source=gname,
                )
                verdicts.append(Verdict(severity=Severity.DENY, detector=gname, detail=detail))
            else:
                # Inert tier-1: tolerate (a broken gate must not break a plain
                # fetch) but record the skip — never a silent fail-open.
                await run.emit(
                    ev.GATE_DECIDED,
                    {"action_ref": str(invoked_id), "tool": call.name, "decision": "skipped",
                     "gate": gname, "rule_id": "gate.crashed.skipped", "detail": detail},
                    parent=invoked_id, source=gname,
                )
            continue
        if verdict is None:
            continue
        payload = {
            "action_ref": str(invoked_id),
            "tool": call.name,
            "decision": verdict.severity.value,
            "gate": verdict.detector,
            "rule_id": verdict.detector,
            "detail": verdict.detail,
        }
        if verdict.kind:
            payload["kind"] = verdict.kind
        await run.emit(ev.GATE_DECIDED, payload, parent=invoked_id, source=verdict.detector)
        verdicts.append(verdict)
    # A gate may have written cumulative state (e.g. a velocity counter); record
    # those writes to the log now so a pause/resume rebuilds them.
    await run.flush_grants(parent=turn)
    return _worst(verdicts)


# --- human-in-the-loop ESCALATE: pause / resume (ZU-CD-1/2/5) -------------


async def _pause_for_human(
    run: _Run, ladder: _Ladder, tokens: int, step: int, call: ToolCall, verdict: Verdict, idem: str,
    *, annotations: dict | None = None,
) -> Result:
    """Suspend the run for a human to approve a specific invocation. The approval
    record shows the LITERAL invocation parameters the harness holds (ground
    truth, never model narration — ZU-CD-1), and the resumable snapshot persists
    the gate-relevant state (tier, tokens, taint, the pending call + its
    idempotency key, and any rail-step ``annotations``) so resume stays bounded
    (ZU-CD-5) and the approved action carries its consequence/destination on the
    log when it finally executes (ZU-RAIL-4)."""
    approval_id = str(uuid5(run.trace_id, f"approval:{idem}"))
    pending: dict = {"tool": call.name, "args": call.args, "idempotency_key": idem}
    if annotations:
        pending["annotations"] = dict(annotations)
    await run.emit(
        ev.APPROVAL_REQUESTED,
        {
            "approval_id": approval_id,
            "tool": call.name,
            "args": call.args,  # the harness's literal parameters, not model text
            "idempotency_key": idem,
            "reason": verdict.detector,
            "detail": verdict.detail,
        },
        parent=run.root,
        source=verdict.detector,
    )
    await run.emit(
        ev.RUN_PAUSED,
        {
            "approval_id": approval_id,
            "tier": ladder.current,
            "tokens": tokens,
            "tainted": run.tainted,
            "step": step,
            "pending": pending,
        },
        parent=run.root,
    )
    return Result(status=Status.PAUSED, reason=approval_id)


def _rebuild_run_state(events: Sequence[Event]) -> dict:
    """Fold a paused run's event log back into its resumable state (ZU-CD-5):
    the root, tier, tokens, taint flag, dispatch counter, durable grant writes,
    and the latest pending approval with its human resolution (if any)."""
    root: UUID | None = None
    tier = 1
    tokens = 0
    tainted = False
    call_seq = 0
    grant_updates: list[tuple[str, str, Any]] = []
    execution_claims: list[str] = []
    pending: dict | None = None
    resolutions: dict[str, dict] = {}
    for e in events:
        t, p = e.type, e.payload
        if t == ev.TASK_STARTED:
            root = e.event_id
            tainted = bool(p.get("tainted", False))
        elif t == ev.TAINT_RAISED:
            tainted = True
        elif t == ev.TASK_ESCALATED and "to_tier" in p:
            tier = max(tier, int(p["to_tier"]))
        elif t == ev.GRANT_UPDATED:
            grant_updates.append((p["grant_id"], p["key"], p["value"]))
        elif t == ev.EXECUTION_CLAIMED:
            execution_claims.append(p["key"])
        elif t == ev.TOOL_INVOKED:
            call_seq += 1
        elif t == ev.RUN_PAUSED:
            tier = max(tier, int(p.get("tier", tier)))
            tokens = int(p.get("tokens", tokens))
            tainted = tainted or bool(p.get("tainted", False))
            pend = p.get("pending")
            pending = {**pend, "approval_id": p.get("approval_id")} if pend else None
        elif t == ev.APPROVAL_RESOLVED:
            aid = p.get("approval_id")
            if aid is not None:
                resolutions[aid] = p
    resolution = resolutions.get(pending["approval_id"]) if pending else None
    return {
        "root": root,
        "tier": tier,
        "tokens": tokens,
        "tainted": tainted,
        "call_seq": call_seq,
        "grant_updates": grant_updates,
        "execution_claims": execution_claims,
        "pending": pending,
        "resolution": resolution,
    }


# --- restore-to-last-known-good rollback (ZU-RAIL-8) ----------------------
#
# Builds on the EXISTING event-sourcing (``_rebuild_run_state``): it does NOT
# invent a parallel snapshot mechanism. The distinction from forward-resume is
# load-bearing — ``_resume_from_log`` keeps the WHOLE log and executes the one
# pending human-approved invocation (it moves FORWARD past a pause); rollback folds
# only the GOOD PREFIX (dropping the failed tail) and executes NOTHING pinned,
# handing control back to the model to choose a DIFFERENT path (it moves BACKWARD
# to a known-good fork point). RETRY severity only re-prompts in place and does not
# roll back state; this primitive is the missing piece it complements.


class _RollbackSeed:
    """Carries a rollback request from ``rollback_and_replan`` into ``run_task``'s
    spine-seeding branch: the prior log and the resolved LKG event to fold to."""

    __slots__ = ("prior", "lkg")

    def __init__(self, prior: Sequence[Event], lkg: UUID) -> None:
        self.prior = prior
        self.lkg = lkg


async def _seed_from_rollback(run: _Run, ladder: _Ladder, seed: _RollbackSeed) -> int:
    """Re-seat ``run`` at the LKG by folding ONLY the good prefix (ZU-RAIL-8), emit
    ``harness.run.rolled_back`` with the dropped-tail count, and return the rebuilt
    token count. Mirrors ``_resume_from_log``'s spine restore (root/tier/tokens/
    taint/dispatch-counter/grant load/claim load) but over the truncated prefix —
    so the failed tail is dropped and consume-once claims from the good prefix are
    preserved."""
    idx = _index_of(seed.prior, seed.lkg)
    dropped = len(seed.prior) - (idx + 1)
    state = _rebuild_to(seed.prior, seed.lkg)
    run.root = state["root"]
    ladder.current = max(ladder.current, int(state["tier"]))
    run.tainted = bool(state["tainted"])
    run._ctx.tainted = run.tainted
    run._call_seq = int(state["call_seq"])
    loader = getattr(run.grant_state, "load", None)
    if loader is not None:
        for grant_id, key, value in state["grant_updates"]:
            loader(grant_id, key, value)
    claim_loader = getattr(run.exec_ledger, "load", None)
    if claim_loader is not None:
        for ckey in state["execution_claims"]:
            claim_loader(ckey)
    await run.emit(ev.RUN_ROLLED_BACK, {"to": str(seed.lkg), "dropped": dropped}, parent=run.root)
    return int(state["tokens"])


def last_known_good(events: Sequence[Event]) -> UUID | None:
    """The event_id of the most recent last-known-good (LKG) marker (ZU-RAIL-8).

    Prefers the latest explicit ``harness.checkpoint.marked``; falling back to the
    latest successfully-returned step (the last ``harness.tool.returned`` with no
    later halting verdict) when none was explicitly marked. Returns ``None`` when
    there is no good point to restore to.
    """
    last_marker: UUID | None = None
    last_returned: UUID | None = None
    for e in events:
        if e.type == ev.CHECKPOINT_MARKED:
            last_marker = e.event_id
        elif e.type == ev.TOOL_RETURNED:
            last_returned = e.event_id
    if last_marker is not None:
        return last_marker
    return last_returned  # the last good return even if a halt followed (the LKG)


def _index_of(events: Sequence[Event], event_id: UUID) -> int:
    for i, e in enumerate(events):
        if e.event_id == event_id:
            return i
    raise KeyError(f"event {event_id} not found in the prior log")


def _rebuild_to(events: Sequence[Event], lkg_id: UUID) -> dict:
    """Fold ONLY the good prefix of the log up to and including the LKG event
    (ZU-RAIL-8). Reuses ``_rebuild_run_state`` over ``events[: index+1]`` — so
    tier/tokens/taint/grant-counters/claimed-set come from the GOOD prefix ONLY and
    the failed tail is dropped. Distinct from ``_rebuild_run_state`` over the full
    log, which would re-seat the failed tail."""
    idx = _index_of(events, lkg_id)
    return _rebuild_run_state(events[: idx + 1])


async def rollback_and_replan(
    spec: TaskSpec,
    provider: ModelProvider,
    *,
    prior: Sequence[Event],
    to: UUID | None = None,
    registry: Registry | None = None,
    bus: EventBus | None = None,
    providers: Mapping[int, ModelProvider] | None = None,
    containment: str = "audit",
    grants: Any = None,
    ledger: Any = None,
    trace_id: UUID | None = None,
    max_observation_chars: int | None = None,
    observation_strategy: str = "truncate",
    max_context_chars: int | None = None,
) -> Result:
    """Re-seat a run at a prior last-known-good event for a DIFFERENT on-rail retry
    (ZU-RAIL-8), then re-enter the model loop so the model picks a new path.

    Rebuilds state to ``to`` (or ``last_known_good(prior)`` when ``None``), emits
    ``harness.run.rolled_back`` {"to", "dropped"}, re-seats the run spine exactly
    as ``_resume_from_log`` does (root/tier/tokens/taint/dispatch-counter/grant
    load/claim load) from the GOOD PREFIX only, and runs the model loop from a fresh
    turn. Consume-once is preserved: claimed keys from the good prefix are re-loaded
    so an already-executed irreversible side effect is NOT re-run, while the dropped
    failed tail's claims are gone.

    The same model-loop options a normal ``run_task`` supports are threaded through
    the re-plan: per-tier ``providers`` (a tier climbed in the good prefix re-enters
    on its bound provider), the ``containment`` floor, and the observation/context
    bounds. The REPLAY-NAVIGATOR kwargs (``track``/``replay_budget``/
    ``finish_provider``/``replay_jitter_median_ms``) are deliberately NOT threaded:
    a rollback exists precisely so the model picks a DIFFERENT path, so re-driving
    the recorded track would re-walk the failed route — they are mutually exclusive
    with a re-plan and are left at their defaults (no replay).
    """
    lkg = to if to is not None else last_known_good(prior)
    return await run_task(
        spec, provider, registry, bus, providers=providers, containment=containment,
        grants=grants, ledger=ledger, trace_id=trace_id,
        max_observation_chars=max_observation_chars, observation_strategy=observation_strategy,
        max_context_chars=max_context_chars,
        _rollback=_RollbackSeed(prior=prior, lkg=lkg) if lkg is not None else None,
    )


async def _resume_from_log(
    run: _Run,
    events: Sequence[Event],
    ladder: _Ladder,
    gates: list | tuple,
    spec: TaskSpec,
    *,
    max_observation_chars: int | None,
    observation_strategy: str,
    provider: ModelProvider,
) -> tuple[Result | None, int, list[dict]]:
    """Resume a paused run from its log (ZU-CD-5). Rebuilds the security spine,
    then resolves the pending approval: an ``approve`` whose idempotency key
    matches the paused invocation executes that EXACT call (ZU-CD-2), unchanged;
    a ``deny`` or a key mismatch blocks it; no resolution yet keeps the run
    paused. Returns ``(paused_or_None, tokens, messages)``."""
    state = _rebuild_run_state(events)
    run.root = state["root"]
    # Restore the gate-relevant spine so the resumed run stays bounded.
    ladder.current = max(ladder.current, int(state["tier"]))
    run.tainted = bool(state["tainted"])
    run._ctx.tainted = run.tainted
    run._call_seq = int(state["call_seq"])
    loader = getattr(run.grant_state, "load", None)
    if loader is not None:
        for grant_id, key, value in state["grant_updates"]:
            loader(grant_id, key, value)
    # Rebuild the consume-once claimed set (ZU-CD-6) so an already-executed approval
    # is seen as taken and a re-resume refuses to run its side effect again.
    claim_loader = getattr(run.exec_ledger, "load", None)
    if claim_loader is not None:
        for ckey in state["execution_claims"]:
            claim_loader(ckey)
    tokens = int(state["tokens"])
    messages = _initial_messages(spec, ladder.active().values())

    pending = state["pending"]
    resolution = state["resolution"]
    await run.emit(
        ev.RUN_RESUMED,
        {"approval_id": pending["approval_id"] if pending else None},
        parent=run.root,
    )
    if pending is None:
        return None, tokens, messages  # nothing pending; just continue the run
    if resolution is None:
        # No human decision yet: stay paused (idempotent — re-resuming re-pauses).
        return Result(status=Status.PAUSED, reason=pending["approval_id"]), tokens, messages

    turn = await run.emit(ev.TURN_STARTED, {"step": 0, "resumed": True}, parent=run.root)
    approved = (
        resolution.get("decision") == "approve"
        and resolution.get("idempotency_key") == pending["idempotency_key"]
    )
    if approved:
        # Execute ONLY the approved invocation, unchanged, bound to its exact key.
        # Carry the rail step's annotations (ZU-RAIL-4) so the approved action's
        # consequence/destination land on the log when it finally executes.
        obs = await _invoke(
            run, turn, ladder.active(), pending["tool"], pending["args"],
            gates=gates, approved_key=pending["idempotency_key"],
            timeout=spec.budget.wall_time_s, annotations=pending.get("annotations"),
        )
        model_obs = await _shrink_for_model(
            obs, max_chars=max_observation_chars, strategy=observation_strategy,
            provider=provider, query=spec.query,
        )
        messages.append(
            {"role": "tool", "name": pending["tool"], "content": json.dumps(model_obs, default=str)}
        )
    else:
        # Denied, or the resolution's key does not bind to this invocation
        # (approve-then-swap defeated): record the block, never execute it.
        await run.emit(
            ev.DEFENSE_BLOCKED,
            {"kind": "human_denied", "tool": pending["tool"],
             "detail": "approval denied or binding mismatch"},
            parent=run.root,
            source="human",
        )
        messages.append(
            {"role": "tool", "name": pending["tool"], "content": json.dumps({"blocked": "human_denied"})}
        )
    return None, tokens, messages


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
