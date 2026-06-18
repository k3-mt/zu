"""Build step 9: the killer demo reaches a working result, with zero setup.

The step's promise is "a new person on a clean machine reaches a working result
in five minutes." These tests stand in for that: the demo runs offline (no key,
no network, no Docker), drives the full three-pillar arc, and ends in a grounded,
schema-valid success — proven two ways, as a subprocess (exactly what a new
person types) and by inspecting the event log the run produced.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from zu_core.contracts import Status

# examples/ is not an installed package; load the demo module by file path the
# way a contributor would run it (`python examples/killer_demo.py`).
_DEMO_PATH = Path(__file__).resolve().parents[3] / "examples" / "killer_demo.py"


def _load_demo():
    spec = importlib.util.spec_from_file_location("killer_demo", _DEMO_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = _load_demo()


@pytest.mark.asyncio
async def test_demo_runs_the_escalation_arc_to_a_grounded_success():
    result, bus, backend = await demo.run_demo(demo._scripted_arc())

    # Pillar 3: a schema-valid, grounded answer (grounding + schema validators
    # are registered, so success means the value was confirmed against the DOM).
    assert result.status is Status.SUCCESS
    assert result.value == {"name": "Acme Widget", "price": "$9.00"}

    events = await bus.query()
    types = [e.type for e in events]

    # Pillar 1: a detector — not the model — drove the climb, recorded as a tier
    # escalation from 1 to 2 (a climb, not a terminal give-up).
    assert "harness.detector.fired" in types
    escalated = [e for e in events if e.type == "harness.task.escalated"]
    assert len(escalated) == 1
    assert escalated[0].payload["reason"] == "js-shell"
    assert (escalated[0].payload["from_tier"], escalated[0].payload["to_tier"]) == (1, 2)
    assert "exhausted" not in escalated[0].payload  # it climbed, did not exhaust

    # The tier-2 browser was actually leased and torn down (no sandbox leak).
    assert backend.launched and backend.destroyed == 1
    assert backend.launched[0]["tier"] == 2

    # Pillar 2: the run is a queryable log that ends in completion.
    assert types[-1] == "harness.task.completed"


def test_demo_main_exits_zero_offline():
    # The default (scripted) path takes no arguments and must succeed offline.
    assert demo.main([]) == 0


def test_demo_runs_as_a_subprocess_the_way_a_new_person_types_it():
    # The literal "clean machine" path: run the file, expect a clean exit and the
    # arc visible in the output. Uses the same interpreter running the tests.
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(_DEMO_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "ESCALATE 1→2" in out
    assert "RESULT   : success" in out
    assert "Acme Widget" in out
