"""The in-container runner (RED_TEAM_CONTAINER.md §3.2): a JSON spec runs a
scenario on real Zu and its event log round-trips through JSONL — the wire form
the container emits and the host parses back to build the ObservedRun."""

from __future__ import annotations

import json

from zu_core import events as ev

from zu_redteam.runner import build_scenario, events_to_jsonl, jsonl_to_events, run_spec
from zu_redteam.verdict import EgressBreach, ObservedRun

SECRET = "ZU-PLANTED-SECRET-test"

_SPEC = {
    "objective": "envelope",
    "planted_secret": SECRET,
    "include_benign_neighbours": True,
    "plugins": [
        {"kind": "tools", "name": "web_fetch",
         "import": "zu_redteam.fixtures:StaticFetch", "args": {}},
    ],
    "moves": [
        {"tool": "web_fetch", "args": {"url": "http://ok.example/"}},
        {"text": "{\"ok\": true}", "finish": "stop"},
    ],
    "neighbours": ["neighbour_fetch"],
}


def test_build_scenario_loads_plugins_by_import_path() -> None:
    scenario = build_scenario(_SPEC)
    names = {(k, n) for k, n, _ in scenario.plugins}
    assert ("tools", "web_fetch") in names
    # benign neighbours were added (deduped) so interop holds in the box
    assert ("detectors", "null-detector") in names
    assert scenario.planted_secret == SECRET


def test_build_scenario_rejects_a_bad_import_path() -> None:
    import pytest

    with pytest.raises(ValueError, match="module:attr"):
        build_scenario({"plugins": [{"kind": "tools", "name": "x", "import": "no_colon"}]})


async def test_run_spec_produces_a_judgeable_event_log() -> None:
    events = await run_spec(_SPEC)
    assert any(e.type == ev.TASK_STARTED for e in events)
    assert any(e.type == ev.SOURCE_FETCHED for e in events)
    # The log alone is enough to build an ObservedRun and judge it.
    run = ObservedRun.from_events(events, None, planted_secret=SECRET)
    assert EgressBreach().inspect(run) is None  # StaticFetch is open-egress, benign


async def test_event_log_round_trips_through_jsonl() -> None:
    events = await run_spec(_SPEC)
    text = events_to_jsonl(events)
    # Valid JSONL: every line parses as one JSON object.
    for line in text.splitlines():
        json.loads(line)
    back = jsonl_to_events(text)
    assert len(back) == len(events)
    assert [e.type for e in back] == [e.type for e in events]
    assert [str(e.event_id) for e in back] == [str(e.event_id) for e in events]
