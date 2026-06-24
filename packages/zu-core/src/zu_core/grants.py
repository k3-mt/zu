"""The in-memory default GrantStore (ZU-CD-4).

Cumulative limits — "$X per hour", "N transactions per window", "spend-so-far" —
need state that survives across invocations, not just the single call. A
``GrantStore`` (the port in ``zu_core.ports``) is a deliberately tiny keyed
get/put scoped by a consumer-supplied ``grant_id``; that poverty (no query, no
iteration, no transactions) is what keeps durable state from bloating the core.

This default is a plain dict plus a small **journal**: every ``put`` records a
``(grant_id, key, value)`` tuple the loop drains and writes to the event log as
``harness.grant.updated``. The log stays the source of truth, so a paused run
rebuilds its counters on resume by folding those events back in via ``load``
(which sets without re-journaling). A durable backing (SQL/Redis) is a plugin
the harness injects instead — it persists itself and needs no journal.
"""

from __future__ import annotations

import threading
from typing import Any


class InMemoryGrantStore:
    name = "memory"

    def __init__(self) -> None:
        self._d: dict[tuple[str, str], Any] = {}
        # Pending (grant_id, key, value) writes the loop drains to the log.
        self._journal: list[tuple[str, str, Any]] = []
        # Guards check-and-increment so a cumulative cap holds under concurrency
        # (multiple tasks / workers sharing a grant). get()+put() is TOCTOU-racy;
        # ``incr_if_below`` is the atomic primitive (ZU-CD-4).
        self._lock = threading.Lock()

    def get(self, grant_id: str, key: str, default: Any = None) -> Any:
        return self._d.get((grant_id, key), default)

    def put(self, grant_id: str, key: str, value: Any) -> None:
        self._d[(grant_id, key)] = value
        self._journal.append((grant_id, key, value))

    def incr_if_below(
        self, grant_id: str, key: str, delta: Any, ceiling: Any, default: Any = 0
    ) -> bool:
        """Atomic check-and-increment for cumulative caps ("$X/window", "N/window").

        If ``current + delta <= ceiling`` store the new value and return ``True``;
        otherwise leave the value unchanged and return ``False``. This is the
        concurrency-safe alternative to ``get`` then ``put``: that pair is
        TOCTOU-racy, so two concurrent invocations can each pass an under-cap check
        and both proceed, overshooting the ceiling (a real over-spend for a money
        grant). A committed increment journals exactly like ``put`` so the event
        log stays the source of truth and resume rebuilds the counter."""
        with self._lock:
            current = self._d.get((grant_id, key), default)
            new = current + delta
            if new > ceiling:
                return False
            self._d[(grant_id, key)] = new
            self._journal.append((grant_id, key, new))
            return True

    def load(self, grant_id: str, key: str, value: Any) -> None:
        """Set a value WITHOUT journaling — used on resume to rebuild counters
        from ``harness.grant.updated`` events without re-emitting them."""
        self._d[(grant_id, key)] = value

    def drain(self) -> list[tuple[str, str, Any]]:
        """Return and clear the pending journal (the loop emits one
        ``harness.grant.updated`` per entry)."""
        out = self._journal[:]
        self._journal.clear()
        return out
