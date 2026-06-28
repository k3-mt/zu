"""Auto-settle — a harness-owned, budget-bounded wait for a browser surface to quiesce
before/after an act (the navigation-reliability layer).

The competitors poll a quiescence oracle (CDP ``Network.*`` in-flight count,
``document.readyState``, a DOM mutation quiet-window) before reading or clicking, and again
after, so a reactive SPA's mutations land before the next step. zu makes that a HARNESS
precondition of acting rather than a model-chosen ``wait_until``/``wait_ms``, and bounds it by
``Budget.settle_ms_max`` so a hostile or buggy page can NEVER stall the run — the
"default to a signal that always terminates" discipline an untrusted-model runtime needs.

This module is the generic, deterministic core. It asks the live session for a cheap
quiescence read (``op=quiescence`` → ``{"quiescent": bool, "fingerprint": str}``) and polls,
bounded, until the surface is quiescent (pre-act) or has STOPPED MUTATING — two equal
fingerprints in a row (post-act, the SPA-settled signal). A session/server that does not
implement the probe degrades gracefully to a single no-op probe (``reason="unsupported"``);
nothing hangs and the act it guards still proceeds.

Determinism (offline, $0): the loop is bounded by an integer poll count derived from the
budget, NOT by reading a wall clock — so a fake session that quiesces after N polls drives the
exact same path every run, replayable with no live browser.
"""

from __future__ import annotations

import asyncio
from typing import Any

SETTLE_POLL_MS = 50  # default gap between quiescence polls


def settle_budget_ms(ctx: Any) -> int:
    """The run's settle budget from ``ctx.spec.budget.settle_ms_max`` — 0 (disabled) when a
    ctx carries no budget, so the settle is inert unless a real run opts in. Defensive: a
    malformed/absent budget yields 0, never a crash."""
    budget = getattr(getattr(ctx, "spec", None), "budget", None)
    try:
        return max(0, int(getattr(budget, "settle_ms_max", 0)))
    except (TypeError, ValueError):
        return 0


async def settle(
    session: Any,
    *,
    budget_ms: int,
    phase: str,
    want_stable: bool = False,
    poll_ms: int = SETTLE_POLL_MS,
) -> dict | None:
    """Poll the session's quiescence signal until the surface settles or the budget runs out.

    ``phase`` is ``"pre"`` (before an act) or ``"post"`` (after it). ``want_stable`` (post)
    additionally accepts two consecutive EQUAL fingerprints as ``"stable"`` — the surface
    stopped mutating, the SPA-settled signal — so a page that keeps a background request alive
    forever still terminates. Returns a settle record (the loop turns it into a
    ``data.settle.waited`` event), or ``None`` when settling is disabled (``budget_ms<=0``) OR
    the session does not implement the quiescence probe — so an un-upgraded server is a
    transparent no-op (no event, no perturbation), exactly as if settling were off.

    Never raises and never hangs: a probe error / unknown-op returns ``None`` after a single
    probe, and the poll count is hard-bounded by ``budget_ms // poll_ms``."""
    if budget_ms <= 0 or poll_ms <= 0:
        return None
    max_polls = max(1, budget_ms // poll_ms)
    last_fp: str | None = None
    polls = 0
    for i in range(max_polls):
        polls = i + 1
        try:
            resp = await session.send({"op": "quiescence"})
        except Exception:  # noqa: BLE001 - a probe must never crash the act it guards
            return None
        # A server that does not implement the probe (no "quiescent" key, or an error) is not
        # a failure — settling is a transparent no-op (None: no record, no perturbation).
        if not isinstance(resp, dict) or resp.get("error") or "quiescent" not in resp:
            return None
        if resp.get("quiescent") is True:
            return {"phase": phase, "ms_waited": polls * poll_ms, "polls": polls,
                    "reason": "quiescent"}
        fp = resp.get("fingerprint")
        if want_stable and isinstance(fp, str) and fp == last_fp:
            return {"phase": phase, "ms_waited": polls * poll_ms, "polls": polls,
                    "reason": "stable"}
        last_fp = fp if isinstance(fp, str) else last_fp
        # Not settled yet — wait a beat before re-probing (the final poll doesn't sleep).
        if i < max_polls - 1:
            await asyncio.sleep(poll_ms / 1000)
    return {"phase": phase, "ms_waited": polls * poll_ms, "polls": polls,
            "reason": "budget_exhausted"}
