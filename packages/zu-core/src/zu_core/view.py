"""View scoping — allowlist-render redaction for any surface that shows events.

The event log is the system of record, but a *window* onto it that crosses a
trust boundary (an SSE feed, a dashboard, a shared viewport) must not become a
data-leak surface. So a view is **default-deny**: only an allowlist of structural
control-plane fields renders verbatim; every other field — free text, fetched
content, extracted values, arbitrary tool-arg values — is summarized to its
type/length/sha256. It does not try to *detect* sensitive content (that is a trap);
it contains by structure, the same posture as the capability envelope.

Applied at the SOURCE, before an event leaves the process, so the window can be
left on in production. ``full=True`` (local console, or an authorized viewer)
renders everything; the default ``full=False`` is the allowlist-render scope.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Payload keys safe to render verbatim across the whole taxonomy: they are the
# runtime's own control plane (names, tiers, verdicts, counts), never content the
# agent read or produced. A key NOT in this set is summarized — default-deny, so
# a new plugin's unknown field leaks nothing until it is explicitly allowlisted.
RENDER_KEYS: frozenset[str] = frozenset({
    "step", "tier", "from_tier", "to_tier", "exhausted",
    "reason", "detail", "tool", "detector", "severity",
    "kind", "status", "model", "usage", "rendered", "count", "selector",
})


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (int, float, bool))


def _summary(v: Any) -> dict:
    """A content-free descriptor of a value: enough to see *that* something is
    there and whether it changed, never *what* it is."""
    if isinstance(v, dict):
        return {"_type": "object", "keys": sorted(map(str, v))[:20], "fields": len(v)}
    if isinstance(v, (list, tuple)):
        return {"_type": "array", "count": len(v)}
    s = v if isinstance(v, str) else str(v)
    digest = hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:12]
    return {"_type": "str", "len": len(s), "sha256": digest}


def scope_payload(payload: dict, *, full: bool = False) -> dict:
    """Allowlist-render a payload: render allowlisted keys and scalars verbatim,
    summarize everything else. Tool-call ``args`` are handled per-arg so a benign
    numeric arg shows while a URL or query value is summarized."""
    if full:
        return payload
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if k in RENDER_KEYS or _is_scalar(v):
            out[k] = v
        elif k == "args" and isinstance(v, dict):
            out[k] = {ak: (av if _is_scalar(av) else _summary(av)) for ak, av in v.items()}
        else:
            out[k] = _summary(v)
    return out


def scope_event(event: Any, *, full: bool = False) -> dict:
    """A JSON-able, scoped view of an event — same shape as ``model_dump(mode=
    'json')`` so a renderer is unchanged, but with the payload allowlist-rendered.
    """
    parent = getattr(event, "parent_id", None)
    return {
        "event_id": str(event.event_id),
        "trace_id": str(event.trace_id),
        "task_id": str(event.task_id),
        "parent_id": str(parent) if parent is not None else None,
        "type": event.type,
        "source": event.source,
        "ts": event.ts.isoformat() if hasattr(event.ts, "isoformat") else str(event.ts),
        "schema_version": getattr(event, "schema_version", 1),
        "payload": scope_payload(getattr(event, "payload", {}) or {}, full=full),
    }
