"""Shared event-store filtering contract.

Both the in-memory default sink and the SQLite sink filter on the same small
allowlist of indexed fields, with the same semantics — a ``None`` value means
"is null" (e.g. ``{"parent_id": None}`` selects root events). Centralising it
here keeps the two sinks honest: they can't drift on which fields are
queryable or on null semantics.
"""

from __future__ import annotations

from typing import Any

# Only these fields may be filtered on. For the SQLite sink this is also the
# injection guard: column names come from here, never from caller input.
ALLOWED_EVENT_FILTERS: frozenset[str] = frozenset(
    {"event_id", "trace_id", "task_id", "parent_id", "type", "source"}
)


def validate_filter(flt: dict[str, Any]) -> None:
    for key in flt:
        if key not in ALLOWED_EVENT_FILTERS:
            raise ValueError(
                f"unknown filter field: {key!r}; allowed: {sorted(ALLOWED_EVENT_FILTERS)}"
            )


def event_matches(event: Any, flt: dict[str, Any]) -> bool:
    """Python-side predicate (used by the in-memory sink). ``None`` -> is-null.

    Comparison is on ``str(...)`` of both sides *on purpose*, so this stays
    bit-for-bit equivalent to the SQLite sink: there, the indexed columns are
    TEXT (a UUID is stored as ``str(uuid)``) and the query binds ``str(value)``.
    Every field in ``ALLOWED_EVENT_FILTERS`` is a str or a UUID — never an int —
    so there is no ``1 == "1"`` ambiguity, and the two sinks cannot drift on
    match semantics. If a numeric field is ever added to the allowlist, revisit
    this: it would need type-aware comparison on both sides.
    """
    for key, value in flt.items():
        actual = getattr(event, key, None)
        if value is None:
            if actual is not None:
                return False
        elif str(actual) != str(value):
            return False
    return True
