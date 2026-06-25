"""Run-end cleanup hooks — a GENERIC seam so the loop can release run-scoped
resources at the end of a run without zu-core knowing what those resources are.

zu-core owns the run lifecycle but must import nothing but pydantic — it cannot
know about browser containers, sockets, or any plugin's per-run state. So instead
of the loop calling a browser teardown directly, a plugin REGISTERS a cleanup hook
here at import time; the loop invokes every registered hook ONCE at true run end
(terminal/escalate/success — never on a human-pause, which suspends the run rather
than ending it). The default is empty, so a run with no registered hook behaves
exactly as before — the seam is inert until a plugin uses it.

The hook is keyed by nothing here (it is global) and is called with the run key
(``str(spec.task_id)``); each hook decides whether it has anything to release for
that run. This keeps the contract one generic string — never a live handle — so it
round-trips capture/replay and never serialises a socket.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# A run-end cleanup hook: given the run key, release any run-scoped resource the
# plugin holds for that run (idempotent; a no-op when it holds nothing).
RunCleanupHook = Callable[[str], Awaitable[None]]

_HOOKS: list[RunCleanupHook] = []


def register_run_cleanup(hook: RunCleanupHook) -> None:
    """Register a run-end cleanup hook (idempotent on the same callable). Called by
    a plugin at import time; the loop invokes it once at every true run end."""
    if hook not in _HOOKS:
        _HOOKS.append(hook)


async def close_run(run_key: str) -> None:
    """Invoke every registered cleanup hook for ``run_key`` — the loop's single
    run-end teardown point. Best-effort: one hook raising never stops the others
    and never raises over the run's own Result."""
    if not run_key:
        return
    for hook in list(_HOOKS):
        try:
            await hook(run_key)
        except Exception:  # noqa: BLE001 — teardown must not raise over a result
            pass
