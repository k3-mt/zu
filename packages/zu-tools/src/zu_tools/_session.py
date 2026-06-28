"""Run-scoped browser-session sharing — the seam that lets one tool open a live
browser and another reuse the SAME page within a run (Engineering Design §11/§12).

A pointer acts on an ALREADY-OPEN page (the one the Action Surface enumerated), so
the two tools must hit the same live container, not a fresh browser with no page.

THE SHARED LOOKUP IS A MODULE-LEVEL REGISTRY, NOT A PER-TOOL BACKEND. The loop
instantiates each discovered Tool class with NO arguments (``zu_core.loop._materialize``),
so ActionSurface, PointerControl and VisionCapture each build their OWN backend with
their OWN private ``_sessions`` dict — there is NO shared state between tool instances.
Putting the run-scoped registry on a per-tool-instance backend therefore shares
nothing in production. The fix: the shared state lives HERE, in a module-level
registry keyed by ``run_key = str(ctx.spec.task_id)``, which every browser-family
tool reaches. The backend still actually opens the live session (via
``open_run_session``/``open_session``); the registry is the cross-tool LOOKUP plus
the harness-side handle_map (handle -> {role,name}) that the opaque-handle indirection
(§11.3) depends on.

Why a string key (and only a string) crosses into ``RunContext``: a run is rebuilt
on resume and a live socket must NEVER be serialised. So ``RunContext`` carries only
``spec.task_id`` (a string); the live handle + handle_map live here, in-process, for
the duration of the run, and are torn down by :func:`close_run` at run end.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _RunEntry:
    """One run's shared browser state: the live session handle every browser-family
    tool acts on, the harness-side handle_map (handle -> {role,name}) the Action
    Surface populates and the pointer/vision resolve against, and a refcount."""

    handle: Any
    handle_map: dict[str, dict] = field(default_factory=dict)
    refcount: int = 1


# The module-level, process-wide run registry. Keyed by run_key (str). This is the
# ONE place all browser-family tools meet — independent of which backend instance
# each tool happens to build.
_RUNS: dict[str, _RunEntry] = {}
# Guards _RUNS against concurrent runs in the same process. The open path is async
# (it awaits the backend), so the lock only brackets the dict mutations, never the
# await — a per-run double-open is avoided by re-checking under the lock.
_LOCK = threading.Lock()


def run_key(ctx: Any) -> str:
    """The run-scoped key a session is shared under: the run's task id (the loop
    defaults ``trace_id`` to it). Empty when there is no spec/task id — then no
    cross-tool sharing happens (the one-shot fallback), which is the pre-existing
    one-shot behaviour."""
    return str(getattr(getattr(ctx, "spec", None), "task_id", "") or "")


async def get_or_open(
    run_key: str, opener: Callable[[], Awaitable[Any]]
) -> Any:
    """Return the run's shared session handle, opening it ONCE on first use.

    ``action_surface(op=open)`` / ``browser(op=open)`` call this: the first call of a
    run actually opens the live session (via ``opener``, which leases the container);
    a later call in the SAME run reuses the registered handle (refcount bumped). A
    second opener racing the first is collapsed under the lock — only one lease wins,
    the loser is closed."""
    if not run_key:
        # No run key -> no shared registry; just open a one-shot session.
        return await opener()
    with _LOCK:
        entry = _RUNS.get(run_key)
        if entry is not None:
            entry.refcount += 1
            return entry.handle
    # Open OUTSIDE the lock (it awaits the backend / container launch).
    handle = await opener()
    with _LOCK:
        entry = _RUNS.get(run_key)
        if entry is not None:
            # Another opener won the race while we were leasing; keep theirs, and
            # discard ours below (outside the lock).
            entry.refcount += 1
            loser = handle
        else:
            _RUNS[run_key] = _RunEntry(handle=handle)
            loser = None
    if loser is not None:
        await _safe_close(loser)
        return _RUNS[run_key].handle
    return handle


def attach(run_key: str) -> Any | None:
    """ATTACH to the run's existing shared session — a PURE READ, no lease, no
    refcount bump. The pointer/vision use this: they must act on the page the Action
    Surface opened, so leasing a fresh empty browser would be wrong. Returns the live
    handle or None when nothing is open for this run.

    Pure read (no refcount change): :func:`close_run` is the single authoritative
    teardown, so attachers never need to release."""
    if not run_key:
        return None
    with _LOCK:
        entry = _RUNS.get(run_key)
        return entry.handle if entry is not None else None


def put_handle_map(run_key: str, handle_map: dict[str, dict]) -> None:
    """Store the run's handle -> {role,name} map (the harness-side indirection the
    model never sees, §11.3). The Action Surface calls this after a reduction; the
    pointer/vision resolve against it. A no-op when there is no run key or no open
    entry (the offline reduce-only path has no live session to attach a map to)."""
    if not run_key:
        return
    with _LOCK:
        entry = _RUNS.get(run_key)
        if entry is not None:
            entry.handle_map = dict(handle_map)


def handle_map_of(run_key: str) -> dict[str, dict]:
    """A COPY of the run's full handle → {role, name} map, in insertion (document) order — the
    raw material for identity re-binding (role+name+nth) when a handle goes stale. Empty when no
    session/map is registered for the run. A copy, so a caller can never mutate the live map."""
    if not run_key:
        return {}
    with _LOCK:
        entry = _RUNS.get(run_key)
        return {h: dict(loc) for h, loc in entry.handle_map.items()} if entry is not None else {}


def resolve_handle(run_key: str, handle: str) -> dict | None:
    """Resolve an opaque handle to its durable ``{role, name}`` locator from the run's
    shared handle_map. The HARNESS does this — the model only ever emits the handle.
    None for an unknown/stale handle (the caller escalates, never crashes, §11.3)."""
    if not run_key or not handle:
        return None
    with _LOCK:
        entry = _RUNS.get(run_key)
        if entry is None:
            return None
        loc = entry.handle_map.get(handle)
        return dict(loc) if loc is not None else None


async def close_run(run_key: str) -> None:
    """Authoritative run-end teardown: close the run's shared session and drop the
    entry. Wired into the loop's run-end lifecycle so a shared container never
    outlives its run. Idempotent — a no-op when nothing is registered."""
    if not run_key:
        return
    with _LOCK:
        entry = _RUNS.pop(run_key, None)
    if entry is not None:
        await _safe_close(entry.handle)


async def _safe_close(handle: Any) -> None:
    close = getattr(handle, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 — teardown must not raise over a result
        pass


# Register run-end teardown with zu-core's GENERIC run-lifecycle seam, so the loop
# releases this run's shared browser session at the end of every run (terminal /
# escalate / success / crash — never a human pause). zu-core never imports browser
# code; it only calls back a registered hook with the run key. Inert until a browser
# tool is imported (which imports this module), so a non-browser run is unaffected.
try:
    from zu_core.runlifecycle import register_run_cleanup as _register_run_cleanup

    _register_run_cleanup(close_run)
except Exception:  # noqa: BLE001 — an older zu-core without the seam: tools still work
    pass
