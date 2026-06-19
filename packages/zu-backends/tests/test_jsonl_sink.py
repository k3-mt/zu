"""jsonl — the append-only trace sink."""

from __future__ import annotations

import json
import uuid

import pytest

from zu_backends.jsonl_sink import JsonlSink
from zu_core.contracts import Event


def _ev(type_: str = "harness.task.started", **payload) -> Event:
    tid = uuid.uuid4()
    return Event(trace_id=tid, task_id=tid, type=type_, source="loop", payload=payload or {"a": 1})


async def test_append_writes_one_json_line_per_event(tmp_path):
    sink = JsonlSink(str(tmp_path / "t.jsonl"))
    await sink.append(_ev())
    await sink.append(_ev("harness.task.completed", value={"x": 1}))
    lines = (tmp_path / "t.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "harness.task.started"
    assert json.loads(lines[1])["payload"]["value"] == {"x": 1}


async def test_round_trip_query_and_count(tmp_path):
    sink = JsonlSink(str(tmp_path / "t.jsonl"))
    e = _ev("data.record.extracted", value={"price": "$9"})
    await sink.append(e)
    got = await sink.query()
    assert len(got) == 1
    assert got[0].type == "data.record.extracted"
    assert got[0].payload == {"value": {"price": "$9"}}
    assert got[0].event_id == e.event_id  # identical round-trip
    assert await sink.count() == 1


async def test_filter_and_after_seq(tmp_path):
    sink = JsonlSink(str(tmp_path / "t.jsonl"))
    a = _ev("harness.task.started")
    b = _ev("harness.task.completed")
    await sink.append(a)
    await sink.append(b)
    # filter by type
    done = await sink.query({"type": "harness.task.completed"})
    assert len(done) == 1 and done[0].type == "harness.task.completed"
    # after_seq treats the line ordinal as the sequence
    assert len(await sink.query(after_seq=1)) == 1
    assert await sink.count() == 2


async def test_query_missing_file_is_empty(tmp_path):
    sink = JsonlSink(str(tmp_path / "nope.jsonl"))
    assert await sink.query() == []
    assert await sink.count() == 0


async def test_unknown_filter_field_rejected(tmp_path):
    sink = JsonlSink(str(tmp_path / "t.jsonl"))
    await sink.append(_ev())
    with pytest.raises(ValueError):
        await sink.query({"not_a_field": "x"})
