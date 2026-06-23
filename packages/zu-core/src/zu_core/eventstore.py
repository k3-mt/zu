"""Shared event-store filtering contract.

Both the in-memory default sink and the SQLite sink filter on the same small
allowlist of indexed fields, with the same semantics — a ``None`` value means
"is null" (e.g. ``{"parent_id": None}`` selects root events). Centralising it
here keeps the two sinks honest: they can't drift on which fields are
queryable or on null semantics.
"""

from __future__ import annotations

from typing import Any

# The core filterable fields — direct columns on the event. For the SQLite sink
# this is also the injection guard: column names come from here, never from
# caller input.
ALLOWED_EVENT_FILTERS: frozenset[str] = frozenset(
    {"event_id", "trace_id", "task_id", "parent_id", "type", "source"}
)

# Consumer-registered filterable fields (ZU-AUDIT-3). A consumer's chain fields
# (``grant_id``, ``consent_ref``, ``capability_id``, ``peer``, ``idempotency_key``)
# live under ``payload["ctx"]``; registering one makes it filterable so "every
# action under grant X" is one query. A sink indexes these out of ``payload["ctx"]``
# (the SQLite sink via a side index table; memory/jsonl via ``event_matches``).
_EXTRA_FILTERS: set[str] = set()


def register_event_filter(field: str) -> None:
    """Register a consumer-defined ``payload["ctx"]`` field as filterable
    (ZU-AUDIT-3). Register before appending events you want indexed."""
    _EXTRA_FILTERS.add(field)


def allowed_filters() -> frozenset[str]:
    """Every filterable field: the core columns plus registered ctx fields."""
    return ALLOWED_EVENT_FILTERS | frozenset(_EXTRA_FILTERS)


def is_extra_filter(key: str) -> bool:
    """True if ``key`` is a registered consumer field (lives in payload['ctx']),
    not a core event column."""
    return key not in ALLOWED_EVENT_FILTERS and key in _EXTRA_FILTERS


def validate_filter(flt: dict[str, Any]) -> None:
    allowed = allowed_filters()
    for key in flt:
        if key not in allowed:
            raise ValueError(
                f"unknown filter field: {key!r}; allowed: {sorted(allowed)}"
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
        if is_extra_filter(key):
            # A consumer field lives under payload["ctx"] (ZU-AUDIT-3), not as a
            # direct attribute.
            ctx = getattr(event, "payload", {}).get("ctx", {})
            actual = ctx.get(key)
        else:
            actual = getattr(event, key, None)
        if value is None:
            if actual is not None:
                return False
        elif str(actual) != str(value):
            return False
    return True
