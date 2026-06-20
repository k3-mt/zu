"""Multi-phase pipelines — the event-sourced way to chain agent runs.

A pipeline lifts a single run's guarantees to the whole sequence: every phase
shares ONE trace and ONE event log, advances only on a validated success, and a
re-run resumes from the log instead of repeating finished work. All offline
(scripted model), so deterministic and keyless.
"""

from __future__ import annotations

from uuid import uuid4

import zu
from zu_core.contracts import Status


def _cfg(*moves, sink: str | None = None) -> dict:
    # validators off: these tests exercise pipeline ORCHESTRATION (trace, gating,
    # resume) on tool-less scripted phases; schema/grounding are covered in
    # zu-checks. "Validated success" still gates — a clean finalise is SUCCESS.
    cfg: dict = {"provider": {"name": "scripted", "script": list(moves)},
                 "plugins": {"validators": []}}
    if sink is not None:
        cfg["event_sink"] = {"driver": "sqlite", "path": sink}
    return cfg


async def test_two_phase_pipeline_passes_value_forward() -> None:
    seen: dict = {}
    pipe = zu.Pipeline(config=_cfg(
        {"text": '{"name": "AeroPress", "price": "$39"}', "finish": "stop"},
        {"text": '{"blurb": "great press"}', "finish": "stop"},
    ))
    pipe.phase("extract", {"query": "extract name and price"})

    def blurb(prev):
        seen["prev"] = prev.value                       # phase 2 consumes phase 1
        return {"query": f"write a blurb for {prev.value['name']}"}

    pipe.phase("blurb", blurb)
    res = await pipe.arun()

    assert res.status is Status.SUCCESS
    assert res.value == {"blurb": "great press"}                 # final phase's value
    assert seen["prev"] == {"name": "AeroPress", "price": "$39"}  # data flowed forward
    assert res.phases["extract"].value == {"name": "AeroPress", "price": "$39"}


async def test_pipeline_stops_on_a_failed_phase() -> None:
    ran: list[str] = []
    pipe = zu.Pipeline(config=_cfg(
        {"text": '{"ok": true}', "finish": "stop"},
        {"text": "truncated", "finish": "length"},      # phase 2 fails (terminal)
    ))
    pipe.phase("one", {"query": "q1"})
    pipe.phase("two", {"query": "q2"})

    def three(prev):
        ran.append("three")
        return {"query": "q3"}

    pipe.phase("three", three)
    res = await pipe.arun()

    assert res.status is not Status.SUCCESS              # the pipeline failed
    assert res.failed_phase == "two"
    assert "three" not in ran                            # phase 3 never built or ran
    assert "three" not in res.phases


async def test_pipeline_is_one_replayable_trace() -> None:
    pipe = zu.Pipeline(config=_cfg(
        {"text": '{"a": 1}', "finish": "stop"},
        {"text": '{"b": 2}', "finish": "stop"},
    ))
    pipe.phase("p1", {"query": "q"}).phase("p2", {"query": "q"})
    res = await pipe.arun()

    assert res.events                                    # the whole pipeline log
    assert all(e.trace_id == pipe.id for e in res.events)   # ONE correlation id
    assert len({e.task_id for e in res.events}) >= 3        # pipeline + 2 phase task_ids
    types = {e.type for e in res.events}
    assert "harness.pipeline.started" in types
    assert "harness.pipeline.completed" in types
    done = {e.payload.get("phase") for e in res.events
            if e.type == "harness.pipeline.phase.completed"}
    assert done == {"p1", "p2"}


async def test_pipeline_resumes_from_the_log(tmp_path) -> None:
    db = str(tmp_path / "pipe.db")
    pid = uuid4()

    # Run 1: phase A succeeds, phase B truncates (fails) → pipeline stops; A is on
    # the durable log, B is not.
    p1 = zu.Pipeline(
        config=_cfg({"text": '{"v": "A"}', "finish": "stop"},
                    {"text": "x", "finish": "length"}, sink=db),
        pipeline_id=pid,
    )
    p1.phase("A", {"query": "qa"}).phase("B", {"query": "qb"})
    r1 = await p1.arun()
    assert r1.status is not Status.SUCCESS and r1.failed_phase == "B"

    # Run 2 (resume): same id + same sink. A is found complete in the log and
    # SKIPPED (its task builder never runs); B re-runs and now succeeds.
    rebuilt: list[str] = []

    def build_a(prev):
        rebuilt.append("A")
        return {"query": "qa"}

    p2 = zu.Pipeline(
        config=_cfg({"text": '{"v": "B-ok"}', "finish": "stop"}, sink=db),
        pipeline_id=pid,
    )
    p2.phase("A", build_a).phase("B", {"query": "qb"})
    r2 = await p2.arun()

    assert r2.status is Status.SUCCESS
    assert r2.value == {"v": "B-ok"}
    assert "A" not in rebuilt                            # A skipped — not re-executed
    assert r2.phases["A"].value == {"v": "A"}            # A's value reused from the log
    assert any(e.type == "harness.pipeline.phase.skipped" for e in r2.events)
    # A was started exactly once across both runs (run 1), never re-run.
    a_starts = [e for e in r2.events
                if e.type == "harness.task.started" and e.payload.get("query") == "qa"]
    assert len(a_starts) == 1
