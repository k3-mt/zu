"""The in-memory default ExecutionLedger — consume-once / idempotent execution (ZU-CD-6).

A human approval (ZU-CD-1/2) authorises *one* irreversible side effect, and that
"exactly once" must survive across component/process lifetimes — a fresh runner
resuming the same resolved approval must NOT execute the side effect a second time.
The obvious place to keep the "already done" flag is a per-instance dict, which a
new instance silently resets; the durable answer is the event log, which Zu already
owns.

An ``ExecutionLedger`` (the port in ``zu_core.ports``) is a deliberately tiny
keyed set with one atomic operation, ``claim(key) -> bool``: the first caller for a
key gets ``True`` (proceed), every later caller — including a replay/resume — gets
``False`` (already executed, refuse). It mirrors ``GrantStore``: this default is a
plain set plus a **journal** the loop drains and writes to the log as
``harness.execution.claimed``; the log stays the source of truth, so a resumed run
rebuilds its claimed set by folding those events back via ``load`` (which records
without re-journaling). A durable backing (SQL ``INSERT ... ON CONFLICT DO NOTHING``,
Redis ``SET NX``) is a plugin the harness injects instead.
"""

from __future__ import annotations

import threading


class InMemoryExecutionLedger:
    name = "memory"

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        # Pending keys the loop drains to the log as ``harness.execution.claimed``.
        self._journal: list[str] = []
        # Makes claim() an atomic test-and-set so two concurrent claims of the same
        # key cannot both win (mirrors InMemoryGrantStore.incr_if_below).
        self._lock = threading.Lock()

    def claim(self, key: str) -> bool:
        """Atomically claim ``key`` for execution: return ``True`` for the first
        caller (proceed) and ``False`` for every later caller (already executed —
        refuse to run the side effect again). A first claim journals so the log
        stays the source of truth and a resumed/replayed run sees the key as taken."""
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            self._journal.append(key)
            return True

    def load(self, key: str) -> None:
        """Mark a key claimed WITHOUT journaling — used on resume to rebuild the
        claimed set from ``harness.execution.claimed`` events without re-emitting."""
        self._claimed.add(key)

    def drain(self) -> list[str]:
        """Return and clear the pending journal (the loop emits one
        ``harness.execution.claimed`` per entry)."""
        out = self._journal[:]
        self._journal.clear()
        return out


__all__ = ["InMemoryExecutionLedger"]
