"""The core orchestration primitive — run_pipeline over TaskSpec phases.

Proves the chaining/gating/one-trace logic at the core level (no config system,
no facade): phases share one trace, advance only on success, and the whole run is
one queryable lineage. The config-driven ergonomics are tested in zu/tests.
"""

from __future__ import annotations

from zu_core.contracts import Result, Status, TaskSpec
from zu_core.pipeline import Phase, run_pipeline
from zu_providers.scripted import ScriptedProvider


def _phase(name: str, query: str) -> Phase:
    return Phase(name, lambda prev: TaskSpec(query=query))


async def test_run_pipeline_chains_under_one_trace() -> None:
    provider = ScriptedProvider.from_moves([
        {"text": '{"a": 1}', "finish": "stop"},
        {"text": '{"b": 2}', "finish": "stop"},
    ])
    res = await run_pipeline([_phase("p1", "q1"), _phase("p2", "q2")], provider)

    assert res.status is Status.SUCCESS
    assert res.value == {"b": 2}                       # final phase's value
    assert all(e.trace_id == res.id for e in res.events)   # one correlated lineage
    assert {e.payload.get("phase") for e in res.events
            if e.type == "harness.pipeline.phase.completed"} == {"p1", "p2"}


async def test_run_pipeline_gates_on_success() -> None:
    seen: list[str] = []

    def p2(prev: Result | None) -> TaskSpec:   # consumes the prior result
        seen.append("p2-built")
        return TaskSpec(query="q2")

    provider = ScriptedProvider.from_moves([
        {"text": "truncated", "finish": "length"},     # p1 fails (terminal)
    ])
    res = await run_pipeline([_phase("p1", "q1"), Phase("p2", p2)], provider)

    assert res.status is not Status.SUCCESS
    assert res.failed_phase == "p1"
    assert "p2-built" not in seen                       # p2 never built or ran
