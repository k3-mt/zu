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
decrypt.

**Threat-model boundary — read this.** Bare hash-chaining detects *partial*
tampering on replay, but it is self-contained: an attacker with write access to
the *whole* store can edit content and **re-link the entire trace cleanly**, so
``verify_chain`` then passes. Two independent, composable mechanisms close that
gap, both stdlib-only:

  1. **External anchoring** (always available, no crypto deps) — periodically
     emit the chain head (``chain_head``) to an append-only *anchor* the consumer
     supplies (a file, an external log, a notary). ``verify_against_anchor``
     re-derives the head at each anchored seq and fails if it differs. A
     full-rewrite attacker cannot edit the external anchor, so the rewrite is
     caught. This is the mechanism that defeats a privileged full rewrite.
  2. **HMAC signing** (only when a signing key is configured) — ``link`` adds an
     HMAC-SHA256 (stdlib ``hmac``) over the digest, and ``verify_chain`` checks it
     when a key is supplied. This protects against an attacker *without the key*
     (they cannot forge a valid signature even after re-linking); it does **not**
     protect against a compromised harness that holds the key. Absent a key,
     behaviour is byte-for-byte unchanged.

Both rest only on the stdlib (``hashlib``/``hmac``/``json``), so the core stays
SDK-free.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Sequence

from .contracts import Event


def event_digest(event: Event, prev_hash: str | None) -> str:
    """The sha256 hex digest binding this event's canonical content to its
    predecessor. The DERIVED chain fields (``hash``/``prev_hash``/``sig``) are
    excluded from the dumped body and the *passed* ``prev_hash`` is injected, so
    the digest is a pure function of (content, predecessor) and recomputable on
    replay. Excluding ``sig`` keeps the content digest byte-for-byte identical
    whether or not the chain is signed — the canonicalization itself is unchanged.
    """
    body = event.model_dump(mode="json", exclude={"hash", "prev_hash", "sig"})
    body["prev_hash"] = prev_hash
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _sign(digest: str, key: bytes) -> str:
    """HMAC-SHA256 over the digest, hex. Symmetric (stdlib only): protects against
    an attacker without ``key``, not against a holder of it."""
    return hmac.new(key, digest.encode("utf-8"), hashlib.sha256).hexdigest()


def link(event: Event, prev_hash: str | None, *, key: bytes | None = None) -> Event:
    """Return a copy of ``event`` with its chain fields set: ``prev_hash`` is the
    predecessor's hash (``None`` for the first event of a trace) and ``hash`` is
    this event's digest. When ``key`` is supplied, ``sig`` is the HMAC over the
    digest; absent a key it stays ``None`` and the result is identical to an
    unsigned chain. The original frozen event is never mutated."""
    digest = event_digest(event, prev_hash)
    sig = _sign(digest, key) if key is not None else None
    return event.model_copy(update={"prev_hash": prev_hash, "hash": digest, "sig": sig})


def verify_chain(
    events: Iterable[Event], *, head: str | None = None, key: bytes | None = None
) -> list[str]:
    """Verify a sequence of linked events (in append/seq order). Returns a list of
    human-readable violation strings; an empty list means the chain is intact.
    ``head`` is the expected ``prev_hash`` of the first event (``None`` for a full
    trace from its root, or a known prior hash to verify a tail). When ``key`` is
    supplied, each event's ``sig`` is additionally checked against the HMAC of its
    digest — so a content edit fails even if the attacker re-linked the chain,
    provided they lack the key. With no key, signatures are not checked and
    behaviour is byte-for-byte as before."""
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
        if key is not None:
            expected = _sign(event.hash, key)
            if event.sig != expected:
                violations.append(
                    f"{event.event_id}: signature mismatch — "
                    "content edited and re-linked without the key, or unsigned"
                )
        prev = event.hash
    return violations


def chain_head(events: Sequence[Event], *, trace_id: object | None = None) -> str | None:
    """The current chain head digest for a trace — the ``hash`` of the last event
    (optionally filtered to ``trace_id``), or ``None`` if there are none. This is
    what a sink/bus emits to an external anchor; pairing the head with its ``seq``
    is what makes a later full rewrite detectable (``verify_against_anchor``)."""
    if trace_id is not None:
        seq_events = [e for e in events if str(e.trace_id) == str(trace_id)]
    else:
        seq_events = list(events)
    return seq_events[-1].hash if seq_events else None


def verify_against_anchor(
    events: Sequence[Event], anchor_records: Iterable[dict]
) -> list[str]:
    """Verify a trace's events against externally-anchored heads. Each anchor
    record is ``{"trace_id", "seq", "head"}`` — the head digest observed after the
    first ``seq`` events of that trace, written to an append-only anchor the
    attacker cannot reach. For each record this RE-DERIVES the canonical head from
    the events (not trusting their stored hashes) and fails if it differs from the
    anchored value. A full-rewrite attacker re-links the chain so ``verify_chain``
    passes, but cannot reproduce the anchored heads — so this catches them.
    Returns a list of violation strings; empty means consistent with the anchor."""
    violations: list[str] = []
    for rec in anchor_records:
        tid = rec.get("trace_id")
        seq = rec.get("seq")
        anchored = rec.get("head")
        trace_events = [e for e in events if str(e.trace_id) == str(tid)]
        if not isinstance(seq, int) or seq < 1 or seq > len(trace_events):
            violations.append(f"anchor for trace {tid}: seq {seq!r} out of range")
            continue
        prev: str | None = None
        for event in trace_events[:seq]:
            prev = event_digest(event, prev)
        if prev != anchored:
            violations.append(
                f"anchor for trace {tid} seq {seq}: re-derived head {prev!r} "
                f"!= anchored {anchored!r} — full rewrite / divergence"
            )
    return violations
