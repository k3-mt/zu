"""The flagship example (``examples/agents/vet-appointment``) and the test-fixture agents
(``packages/zu-cli/tests/agents/``: the custom-tool bundle + the multi-phase pipeline) are
real, runnable, and tested without a key or network:

* **validity** — the shipped ``agent.yaml`` parses and its tier tools + plugins resolve, so a
  copy-pasted example never greets a user with a config error.
* **behaviour** — the agent runs OFFLINE through the real interpreter loop with the real
  tools + validators (``http_fetch``/``html_parse`` + ``schema``/``grounding``) over its
  saved fixture page and a scripted model, proving the task + schema + grounding contract
  holds with no key and no network.

This is the unit/integration lane; the docker lane (validation/containment) runs one inside
the container.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from zu_checks.validators.grounding import GroundingValidator
from zu_checks.validators.schema import SchemaValidator
from zu_cli.config import build_registry, load_agent
from zu_core.contracts import Status
from zu_testing import fetch_tool, search_tool
from zu_tools.parse import HtmlParse

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "examples" / "agents"   # the shipped example
_FIXTURE_AGENTS = Path(__file__).resolve().parent / "agents"                # test-only fixtures


_VET_URL = "https://www.parkvets.example/chislehurst/book"
# Slots only — the agent's output_schema is strict (additionalProperties:false),
# so a volunteered extra (a booking URL) is rejected, not returned. Every string
# returned must ground against retrieved page content; a URL would not.
_VET_ANSWER = {
    "slots": [
        {"date": "2026-06-24", "time": "09:20"},
        {"date": "2026-06-24", "time": "11:40"},
        {"date": "2026-06-25", "time": "14:10"},
    ],
}


def test_vet_appointment_agent_is_valid() -> None:
    # The open-web agent: search at tier 1, browser at tier 2, grounded output.
    d = _AGENTS_DIR / "vet-appointment"
    spec, cfg = load_agent(str(d / "agent.yaml"))
    assert spec.query and spec.output_schema and spec.max_tier == 2
    assert "web_search" in cfg.tiers[1] and "render_dom" in cfg.tiers[2]
    reg = build_registry(cfg)                       # raises on any unknown tool/plugin
    assert {"web_search", "http_fetch", "render_dom"} <= set(reg.names("tools"))
    assert (d / "fixtures" / "booking.html").is_file()


async def test_vet_appointment_searches_fetches_and_grounds(agent_runner) -> None:
    # The full shape: web_search -> http_fetch -> 3 grounded slots. The strict
    # schema returns ONLY slots; each date+time grounds against the page.
    d = _AGENTS_DIR / "vet-appointment"
    spec, _cfg = load_agent(str(d / "agent.yaml"))
    html = (d / "fixtures" / "booking.html").read_text(encoding="utf-8")

    result, events = await agent_runner(
        [{"tool": "web_search", "args": {"query": "Park Vets Chislehurst online booking"}},
         {"tool": "http_fetch", "args": {"url": _VET_URL}},
         {"text": json.dumps(_VET_ANSWER), "finish": "stop"}],
        tools={
            "web_search": search_tool([{"title": "Park Vets Chislehurst — Book", "url": _VET_URL}]),
            "http_fetch": fetch_tool(text=html),
            "html_parse": HtmlParse(),
        },
        validators={"schema": SchemaValidator(), "grounding": GroundingValidator()},
        spec=spec,
    )

    assert result.status is Status.SUCCESS          # schema (3 slots) + grounding passed
    assert result.value == _VET_ANSWER
    # both retrievals are on the log as provenance
    sources = [e for e in events if e.type == "data.source.fetched"]
    assert len(sources) == 2                         # the search results AND the page


async def test_vet_appointment_invented_slot_is_refused(agent_runner) -> None:
    # A time that is NOT on the page must fail grounding — no fabricated bookings.
    d = _AGENTS_DIR / "vet-appointment"
    spec, _cfg = load_agent(str(d / "agent.yaml"))
    html = (d / "fixtures" / "booking.html").read_text(encoding="utf-8")
    bogus = json.loads(json.dumps(_VET_ANSWER))
    bogus["slots"][0]["time"] = "23:59"             # never offered on the page

    result, _events = await agent_runner(
        [{"tool": "web_search", "args": {"query": "q"}},
         {"tool": "http_fetch", "args": {"url": _VET_URL}},
         {"text": json.dumps(bogus), "finish": "stop"}],
        tools={
            "web_search": search_tool([{"title": "Park Vets", "url": _VET_URL}]),
            "http_fetch": fetch_tool(text=html),
            "html_parse": HtmlParse(),
        },
        validators={"schema": SchemaValidator(), "grounding": GroundingValidator()},
        spec=spec,
    )
    assert result.status is not Status.SUCCESS


def test_custom_tool_bundle_runs_via_cli() -> None:
    # The bundle example (examples/agents/custom-tool): a directory with agent.yaml
    # + a tools/ package. Running the DIR loads its own tool (placed at a tier by
    # import-ref) and the scripted agent succeeds — offline, no key.
    import sys

    from typer.testing import CliRunner

    from zu_cli.main import app

    # A bundle's generic `tools` package is cached in sys.modules; drop any stale
    # one so this bundle's tools/ resolves (normal usage is one bundle per process).
    for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
        del sys.modules[m]
    bundle = _FIXTURE_AGENTS / "custom-tool"
    try:
        # --no-track: don't write a learned track.json into the source tree.
        result = CliRunner().invoke(app, ["run", str(bundle), "--no-track"])
        assert result.exit_code == 0, result.output
        assert "status : success" in result.output
        assert "greeting" in result.output
    finally:
        for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            del sys.modules[m]


def test_research_pipeline_example_runs_offline() -> None:
    # The multi-phase example (examples/agents/research-pipeline) runs end to end
    # with the scripted model — gated transitions, one replayable trace, no key.
    script = _FIXTURE_AGENTS / "research-pipeline" / "pipeline.py"
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "status : success" in proc.stdout
    assert "one replayable log" in proc.stdout      # the whole pipeline is one trace
