"""The human-handoff queue — paused runs wait here for an operator (§3.4).

A run that hits a ``kind="human"`` detector/gate (a captcha wall, a declared
human-only step) does not fail and does not spin: the loop emits
``approval.requested`` + ``run.paused`` and returns ``Status.PAUSED``. THIS queue
is where that paused run waits. An operator works the queue through the handoff
API (``/runs/{id}/pending`` to see what is needed, ``/runs/{id}/resolve`` to
submit the human's decision), and the run resumes from EXACTLY where it paused via
``run_task(resume_from=...)`` — the same event-sourced resume the core proves
(ZU-CD-2/6: key-bound, consume-once).

Three disciplines are load-bearing here and are NOT a tight loop:

  * ASYNC with a TIMEOUT and a DEFER path. A paused run carries a deadline; an
    operator can DEFER it (push the deadline out) rather than being forced to
    decide now. A run past its deadline is reported ``expired`` — never silently
    auto-approved (the safe failure for a human gate is to NOT act).
  * REDACTION on everything the queue surfaces. The pending invocation's args and
    any live-view ride through the Shadow redaction discipline
    (``zu_shadow.redaction``) before an operator ever sees them, so a secret in a
    URL/arg never leaks through the console (ZU-AUDIT-4, reused).
  * ROUTE, NEVER DEFEAT. For a captcha the queue presents the challenge to a
    PERSON entitled to operate the system; Zu ships no solver.

The queue holds the live run context (provider/registry/bus) so a resume is a
real continuation, not a re-run. It is in-process (one server) — durable handoff
across processes is a future extension; the durable substrate (the event log) is
already there.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from zu_core import events as ev
from zu_core.contracts import Event, Status, TaskSpec

# Default seconds an escalation waits before it is reported ``expired`` (the
# operator can DEFER to extend). A human gate that no one answers must surface as
# unhandled, never time out into an action.
DEFAULT_TIMEOUT_S = 30 * 60.0


def _redact(payload: dict) -> dict:
    """Redact anything surfaced to an operator using the Shadow discipline — the
    same default-on stage that protects the audit log. Imported lazily so the
    handoff surface has no hard zu-shadow import at module load (zu-cli depends on
    zu-shadow only for this one-way wiring)."""
    try:
        from zu_shadow.redaction import redact_payload
    except ModuleNotFoundError:  # pragma: no cover - zu-shadow always installed in-workspace
        return payload
    return redact_payload(payload)


@dataclass
class PausedRun:
    """One paused run waiting for a human. Holds the live run context so a resume
    is a true continuation (event-sourced), plus the pending invocation the human
    must approve and the deadline/defer state."""

    run_id: str
    spec: TaskSpec
    provider: Any
    registry: Any
    bus: Any
    providers: dict
    run_kwargs: dict
    events: list[Event]
    approval_id: str
    pending: dict  # {"tool", "args", "idempotency_key", ...} — the literal invocation
    reason: str
    detail: str | None
    created_at: float = field(default_factory=time.monotonic)
    deadline: float = 0.0
    status: str = "pending"  # pending | resolved | deferred | expired
    resolution: dict | None = None

    def is_expired(self, now: float) -> bool:
        return self.status == "pending" and now >= self.deadline

    def public_view(self) -> dict:
        """What ``/pending`` and the console surface — REDACTED. The literal args
        the human needs (e.g. the captcha URL) are shown, but swept for secrets
        first; the idempotency key is shown so the resolution can bind to it."""
        now = time.monotonic()
        status = "expired" if self.is_expired(now) else self.status
        return {
            "run_id": self.run_id,
            "approval_id": self.approval_id,
            "tool": self.pending.get("tool"),
            "args": _redact(dict(self.pending.get("args") or {})),
            "idempotency_key": self.pending.get("idempotency_key"),
            "reason": self.reason,
            "detail": self.detail,
            "status": status,
            "needs": _needs_for(self.reason, self.detail),
            "seconds_remaining": max(0.0, round(self.deadline - now, 1)),
        }


def _needs_for(reason: str, detail: str | None) -> str:
    """A short, human-facing description of what the operator must do — enough to
    present the challenge WITHOUT telling them to defeat it (route, not defeat)."""
    if reason == "captcha":
        return ("A captcha / anti-bot wall blocked an authorized step. Complete the "
                "challenge yourself on the target system, then approve to continue. "
                "Zu does not solve captchas.")
    if reason in ("human-gate", "approver", "replay_arbiter"):
        return (detail or "A declared human-only step needs your explicit approval "
                "before it runs.")
    return detail or "This run is paused awaiting a human decision."


class HandoffQueue:
    """An async, in-process queue of paused runs. Operators work it through the
    handoff API; runs carry deadlines and can be deferred. NEVER a synchronous
    blocking loop — the API is poll/resolve, and a deadline bounds the wait."""

    def __init__(self, *, default_timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._runs: dict[str, PausedRun] = {}
        self._lock = asyncio.Lock()
        self._resolve_locks: dict[str, asyncio.Lock] = {}
        self._timeout_s = default_timeout_s

    def resolve_lock(self, run_id: str) -> asyncio.Lock:
        """A per-run lock serialising the WHOLE resolve critical section. A concurrent
        double-resolve of one run cannot both query the log before the first's
        EXECUTION_CLAIMED lands, so consume-once (ZU-CD-6) cannot be raced; the loser
        re-checks existence inside the lock and 404s. Stable per run_id for the process
        lifetime (the same lock still covers a run that pauses again)."""
        return self._resolve_locks.setdefault(run_id, asyncio.Lock())

    async def enqueue(self, paused: PausedRun, *, timeout_s: float | None = None) -> None:
        paused.deadline = paused.created_at + (
            timeout_s if timeout_s is not None else self._timeout_s
        )
        async with self._lock:
            self._runs[paused.run_id] = paused

    async def get(self, run_id: str) -> PausedRun | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def list_pending(self) -> list[dict]:
        """Every queued run's redacted public view, oldest first (the order an
        operator should work them). Expired/resolved/deferred are included with
        their status so the console can show the whole board."""
        async with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: r.created_at)
        return [r.public_view() for r in runs]

    async def defer(self, run_id: str, *, extra_s: float) -> PausedRun | None:
        """Push a run's deadline out — the operator can't decide now but isn't
        giving up. Never auto-approves; just extends the wait."""
        async with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            run.deadline = time.monotonic() + extra_s
            run.status = "pending"  # back to actionable, just with a later deadline
            return run

    async def pop(self, run_id: str) -> PausedRun | None:
        """Remove a run from the queue (after it has been resumed)."""
        async with self._lock:
            return self._runs.pop(run_id, None)


def build_resolution_event(paused: PausedRun, decision: str, by: str,
                           idempotency_key: str | None = None) -> Event:
    """Build the ``approval.resolved`` event the human's decision is recorded as —
    bound to the EXACT paused invocation by ``approval_id`` AND its idempotency key
    (ZU-CD-2). A resolution whose key does not match the pending invocation will be
    rejected by the loop on resume (approve-then-swap defeated); we default the key
    to the pending one so a faithful operator resolution always binds."""
    key = idempotency_key if idempotency_key is not None else paused.pending.get("idempotency_key")
    return Event(
        trace_id=paused.spec.task_id,
        task_id=paused.spec.task_id,
        type=ev.APPROVAL_RESOLVED,
        source="human",
        payload={
            "approval_id": paused.approval_id,
            "decision": decision,
            "idempotency_key": key,
            "by": by,
        },
    )


def paused_from_result(
    run_id: str,
    result: Any,
    *,
    spec: TaskSpec,
    provider: Any,
    registry: Any,
    bus: Any,
    providers: dict,
    run_kwargs: dict,
    events: list[Event],
) -> PausedRun | None:
    """Build a :class:`PausedRun` from a ``Status.PAUSED`` Result + the run's event
    log. Reads the LITERAL pending invocation from ``approval.requested`` /
    ``run.paused`` on the log (ground truth, never model narration). Returns None if
    the result is not paused or the log carries no pending approval."""
    if getattr(result, "status", None) is not Status.PAUSED:
        return None
    requested = [e for e in events if e.type == ev.APPROVAL_REQUESTED]
    paused_ev = [e for e in events if e.type == ev.RUN_PAUSED]
    if not requested or not paused_ev:
        return None
    req = requested[-1].payload
    pend = paused_ev[-1].payload.get("pending", {})
    pending = {
        "tool": req.get("tool") or pend.get("tool"),
        "args": req.get("args") or pend.get("args") or {},
        "idempotency_key": req.get("idempotency_key") or pend.get("idempotency_key"),
    }
    return PausedRun(
        run_id=run_id,
        spec=spec,
        provider=provider,
        registry=registry,
        bus=bus,
        providers=providers,
        run_kwargs=dict(run_kwargs),
        events=list(events),
        approval_id=req.get("approval_id") or result.reason or str(uuid4()),
        pending=pending,
        reason=req.get("reason") or "human",
        detail=req.get("detail"),
    )


def run_id_for(spec: TaskSpec) -> str:
    """The handoff run id is the run's task/trace id — the same id the event log is
    keyed on, so ``/runs/{id}/...`` addresses the run on the canonical log."""
    return str(spec.task_id)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "HandoffQueue",
    "PausedRun",
    "build_resolution_event",
    "paused_from_result",
    "run_id_for",
]
