"""ZU-AUDIT-1 — the event log is append-only AND tamper-evident.

Proves the per-trace hash chain: a clean log verifies; editing a payload,
deleting an event, or reordering events is detected on replay. ZU-AUDIT-3 — a
consumer-registered ``payload["ctx"]`` field is queryable on the in-memory sink.
"""

from __future__ import annotations

from uuid import uuid4

from zu_core.chain import verify_chain
from zu_core.contracts import Event
from zu_core.eventstore import register_event_filter
from zu_core.sinks import MemoryEventSink


def _event(trace, task, type="harness.task.started", **payload) -> Event:
    return Event(
        trace_id=trace,
        task_id=task,
        type=type,
        source="loop",
        payload=payload or {"k": "v"},
    )


async def _log(sink, trace, task, n=4) -> list[Event]:
    out = []
    for i in range(n):
        stored = await sink.append(_event(trace, task, i=i))
        out.append(stored)
    return out


async def test_clean_chain_verifies() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    assert all(e.hash is not None for e in events)
    assert events[0].prev_hash is None  # first event of the trace roots the chain
    assert verify_chain(events) == []  # intact


async def test_content_tamper_detected() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    # Edit a stored event's payload (e.g. hide what was actually done).
    events[2] = events[2].model_copy(update={"payload": {"k": "evil"}})
    violations = verify_chain(events)
    assert any("content tamper" in v for v in violations)


async def test_deletion_detected() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    # Drop the middle event: the next event's prev_hash no longer matches.
    pruned = events[:2] + events[3:]
    violations = verify_chain(pruned)
    assert any("prev_hash break" in v for v in violations)


async def test_reorder_detected() -> None:
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task)
    events = await sink.query({"trace_id": trace})
    swapped = [events[0], events[2], events[1], events[3]]
    assert verify_chain(swapped) != []


async def test_chains_are_per_trace_independent() -> None:
    sink = MemoryEventSink()
    t1, t2, task = uuid4(), uuid4(), uuid4()
    # Interleave two traces; each chain must verify on its own.
    await sink.append(_event(t1, task, i=0))
    await sink.append(_event(t2, task, i=0))
    await sink.append(_event(t1, task, i=1))
    await sink.append(_event(t2, task, i=1))
    assert verify_chain(await sink.query({"trace_id": t1})) == []
    assert verify_chain(await sink.query({"trace_id": t2})) == []


async def test_consumer_field_is_queryable() -> None:
    # ZU-AUDIT-3: a consumer registers a payload["ctx"] field and filters on it.
    register_event_filter("grant_id")
    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await sink.append(_event(trace, task, ctx={"grant_id": "G-1"}))
    await sink.append(_event(trace, task, ctx={"grant_id": "G-2"}))
    await sink.append(_event(trace, task, ctx={"grant_id": "G-1"}))
    rows = await sink.query({"grant_id": "G-1"})
    assert len(rows) == 2
    assert all(r.payload["ctx"]["grant_id"] == "G-1" for r in rows)


# --- ZU-AUDIT-1: anchoring defeats a privileged FULL rewrite -----------------


async def test_full_rewrite_passes_self_verify_but_fails_anchor() -> None:
    from zu_core.chain import chain_head, link, verify_against_anchor

    sink = MemoryEventSink()
    trace, task = uuid4(), uuid4()
    await _log(sink, trace, task, n=4)
    events = await sink.query({"trace_id": trace})

    # Anchor the genuine head at the current seq (what an external notary records).
    anchor_records = [
        {"trace_id": str(trace), "seq": len(events), "head": chain_head(events, trace_id=trace)}
    ]
    assert verify_chain(events) == []
    assert verify_against_anchor(events, anchor_records) == []  # consistent with anchor

    # Attacker with full write access edits content and RE-LINKS the whole trace
    # cleanly, so the self-contained chain verifies...
    rewritten: list[Event] = []
    prev = None
    for i, e in enumerate(events):
        base = e.model_copy(
            update={"payload": {"k": "EVIL"} if i == 1 else e.payload, "hash": None, "prev_hash": None}
        )
        linked = link(base, prev)
        rewritten.append(linked)
        prev = linked.hash
    assert verify_chain(rewritten) == []  # self-verify PASSES — the rewrite is clean

    # ...but the external anchor catches it (the attacker cannot edit the anchor).
    violations = verify_against_anchor(rewritten, anchor_records)
    assert violations and any("full rewrite" in v for v in violations)


def test_hmac_chain_detects_edit_without_key_knowledge() -> None:
    from zu_core.chain import link

    KEY = b"a-signing-secret-the-attacker-lacks"
    trace, task = uuid4(), uuid4()

    # A signed chain.
    signed: list[Event] = []
    prev = None
    for i in range(3):
        linked = link(_event(trace, task, i=i), prev, key=KEY)
        signed.append(linked)
        prev = linked.hash
    assert verify_chain(signed, key=KEY) == []  # intact + signatures valid

    # Attacker edits content and re-links cleanly, but WITHOUT the key (sig=None).
    rebuilt: list[Event] = []
    prev = None
    for i, e in enumerate(signed):
        base = e.model_copy(
            update={"payload": {"k": "EVIL"} if i == 1 else e.payload,
                    "hash": None, "prev_hash": None, "sig": None}
        )
        linked = link(base, prev)  # no key -> cannot forge a valid signature
        rebuilt.append(linked)
        prev = linked.hash
    # The hashes re-link cleanly, but the key-holder's verify catches the bad sig.
    viol = verify_chain(rebuilt, key=KEY)
    assert any("signature mismatch" in v for v in viol)

    # Without a key configured: signed behaviour is absent, and a (non-relinked)
    # content edit is still caught by the existing digest check.
    plain = link(_event(trace, task, i=9), None)
    assert verify_chain([plain]) == []  # unsigned clean chain verifies as before
    edited = plain.model_copy(update={"payload": {"k": "EVIL"}})  # edit, no re-link
    assert any("content tamper" in v for v in verify_chain([edited]))
