"""Standalone, PURE monitor folding — run compiled Monitors over an arbitrary event
sequence and reduce to the worst :class:`MonitorVerdict`, with NO loop and NO I/O.

The effect-verification + monitor machinery is pure and complete, but until now the
ONLY runner that folded compiled Monitors over an event stream was the loop-private
async coroutine ``loop._monitor_checkpoint``. There was no public, synchronous,
loop-free way to evaluate a list of monitors (or declared invariants) over a saved
event log offline. This module is that thin convenience seam: a single ``run_monitors``
implementation the loop now also delegates to, so there is exactly ONE place that
"evaluate each monitor + pick the worst verdict" lives.

It owns only the policy-NEUTRAL reduction (OK/WARN/VIOLATION, worst-wins with
crash-isolation). The loop keeps owning what a verdict MEANS for the run: emitting
``harness.monitor.fired``, the VIOLATION→TERMINAL bridge, and ESCALATE/TERMINAL
gating. Pure: a function of the event history, no model, no network, no clock.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, cast

from .ports import Monitor, MonitorState, MonitorVerdict, RunContext

log = logging.getLogger("zu.monitors")

# Severity ordering for the policy-neutral Monitor vocabulary: a VIOLATION is worse
# than a WARN. Kept here (not the loop's ``_RANK`` over ``Severity``) so the pure
# reduction needs no Verdict/Severity — the loop owns the Monitor→Severity bridge.
_MONITOR_RANK: dict[MonitorState, int] = {
    MonitorState.WARN: 0,
    MonitorState.VIOLATION: 1,
}


def _safe_evaluate(monitor: Any, ctx: RunContext) -> MonitorVerdict | None:
    """Run a Monitor in isolation (ZU-RAIL-5): a raising third-party monitor is
    logged and skipped, never allowed to crash the fold — mirrors the loop's
    ``_safe_evaluate``. A Monitor is pure, but a buggy one must not take down the run."""
    try:
        verdict: MonitorVerdict | None = monitor.evaluate(ctx)
        return verdict
    except Exception as exc:  # noqa: BLE001 - a broken monitor must not halt the run
        log.warning(
            "monitor %r raised %s: %s — skipping it",
            getattr(monitor, "name", monitor), type(exc).__name__, exc,
        )
        return None


def worst_verdict(verdicts: Sequence[MonitorVerdict]) -> MonitorVerdict | None:
    """Reduce fired verdicts to the WORST (VIOLATION > WARN), or ``None`` if empty.
    The single ranking the standalone fold and the loop's checkpoint both use."""
    return max(verdicts, key=lambda v: _MONITOR_RANK[v.state], default=None)


def fold_monitors(
    monitors: Sequence[Monitor], ctx: RunContext
) -> list[MonitorVerdict]:
    """Evaluate every monitor against ``ctx`` under crash-isolation and return the
    non-OK verdicts in monitor order. The ONE "evaluate each monitor" implementation
    the loop's checkpoint also drives (it then emits + bridges each); ``run_monitors``
    builds the ctx and reduces. No emission, no I/O — pure."""
    fired: list[MonitorVerdict] = []
    for m in monitors:
        mv = _safe_evaluate(m, ctx)
        if mv is None or mv.state == MonitorState.OK:
            continue
        fired.append(mv)
    return fired


def run_monitors(
    monitors: Sequence[Monitor], events: Sequence[Any], *, spec: Any = None
) -> MonitorVerdict | None:
    """Fold every Monitor over ``events`` and return the WORST non-OK verdict
    (VIOLATION > WARN), or ``None`` when nothing fired.

    PURE and synchronous: builds ONE minimal :class:`RunContext` (``spec`` plus the
    event sequence — the only fields ``Monitor.evaluate`` reads) and evaluates each
    monitor inside the same crash-isolation the loop uses (a raising monitor is logged
    and dropped). OK/``None`` verdicts are dropped. This is the single implementation
    the loop's monitor checkpoint also delegates to — there is no second copy."""
    if not monitors:
        return None
    ctx = RunContext(spec=spec, events=events)
    return worst_verdict(fold_monitors(monitors, ctx))


def evaluate_invariants(
    invariants: Sequence[Any], events: Sequence[Any], *, spec: Any = None
) -> MonitorVerdict | None:
    """Compile declared :class:`Invariant`\\ s (ZU-RAIL-6) and fold them over
    ``events``, returning the worst verdict or ``None``. The one-liner that turns a
    list of invariants-as-data into the same standalone monitor fold."""
    from .invariants import compile_spec

    # ``compile_spec`` yields ``_CompiledInvariant``s, which satisfy the ``Monitor``
    # structural Protocol (``name`` + ``evaluate(ctx)``); cast for the invariant
    # ``list`` → ``Sequence[Monitor]`` so the one fold serves invariants too.
    compiled = cast("Sequence[Monitor]", compile_spec(list(invariants)))
    return run_monitors(compiled, events, spec=spec)
