"""The tamper-evidence hash chain (ZU-AUDIT-1).

The event log is the system of record and the artifact that proves "acted within
granted authority". Append-only is not enough: a rewritable log makes that claim
fiction. This module adds a per-trace **hash chain** — each event carries a
``hash`` over its own canonical content plus the predecessor's ``hash`` — so any
modification is detectable on replay:

  * **content edit** — recomputing the digest no longer matches the stored
    ``hash`` (caught even when the log is plaintext);
  * **reorder / insert / delete** — an event's ``prev_hash`` no longer matches
    the actual predecessor's ``hash``.

The chain is computed by the **canonical sink at append time** (the single
ordering authority), not by an emitter — see ``EventBus.publish``, which links
once at the canonical store and fans the linked event out to shippers. It
composes with the optional AEAD payload codec: the chain detects row-level
tampering/deletion; AEAD additionally makes an at-rest payload edit fail to
decrypt. Both rest only on the stdlib (``hashlib``/``json``), so the core stays
SDK-free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from .contracts import Event


def event_digest(event: Event, prev_hash: str | None) -> str:
    """The sha256 hex digest binding this event's canonical content to its
    predecessor. ``hash``/``prev_hash`` are excluded from the dumped body and the
    *passed* ``prev_hash`` is injected, so the digest is a pure function of
    (content, predecessor) and recomputable on replay."""
    body = event.model_dump(mode="json", exclude={"hash", "prev_hash"})
    body["prev_hash"] = prev_hash
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def link(event: Event, prev_hash: str | None) -> Event:
    """Return a copy of ``event`` with its chain fields set: ``prev_hash`` is the
    predecessor's hash (``None`` for the first event of a trace) and ``hash`` is
    this event's digest. The original frozen event is never mutated."""
    return event.model_copy(update={"prev_hash": prev_hash, "hash": event_digest(event, prev_hash)})


def verify_chain(events: Iterable[Event], *, head: str | None = None) -> list[str]:
    """Verify a sequence of linked events (in append/seq order). Returns a list of
    human-readable violation strings; an empty list means the chain is intact.
    ``head`` is the expected ``prev_hash`` of the first event (``None`` for a
    full trace from its root, or a known prior hash to verify a tail)."""
    violations: list[str] = []
    prev = head
    for event in events:
        if event.hash is None:
            violations.append(f"{event.event_id}: event is not linked (no hash)")
            prev = event.hash
            continue
        if event.prev_hash != prev:
            violations.append(
                f"{event.event_id}: prev_hash break "
                f"(expected {prev!r}, found {event.prev_hash!r}) — reorder/insert/delete"
            )
        if event.hash != event_digest(event, event.prev_hash):
            violations.append(f"{event.event_id}: content tamper (hash mismatch)")
        prev = event.hash
    return violations
