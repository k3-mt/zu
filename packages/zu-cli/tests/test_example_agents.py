"""The example agents (examples/agents/) are real, runnable, and tested two ways:

* **validity** — the shipped ``task.yaml`` / ``zu.yaml`` parse and their named
  plugins resolve, so a copy-pasted example never greets a user with a config
  error.
* **behaviour** — each agent runs OFFLINE through the real interpreter loop with
  the real tools + validators (``http_fetch``/``html_parse`` + ``schema``/
  ``grounding``) over its saved fixture page and a scripted model, proving the
  task + schema + grounding contract holds with no key and no network.

This is the unit/integration lane for shipped agents; the docker lane
(validation/containment) runs one inside the container.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from zu_checks.validators.grounding import GroundingValidator
from zu_checks.validators.schema import SchemaValidator
from zu_cli.config import build_registry, load_config, load_task
from zu_core.contracts import Status
from zu_testing import fetch_tool
from zu_tools.parse import HtmlParse

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "examples" / "agents"

# Each agent: its dir, fixture page, and the grounded answer present in that page.
_AGENTS = [
    pytest.param(
        "price-extractor", "product.html",
        {"name": "AeroPress Go Travel Coffee Press", "price": "$39.95"},
        id="price-extractor",
    ),
    pytest.param(
        "article-summary", "article.html",
        {"title": "Event sourcing for agents",
         "headings": ["Why a log beats a snapshot", "Deriving views", "Replaying a run"]},
        id="article-summary",
    ),
]


@pytest.mark.parametrize("name, fixture, answer", _AGENTS)
def test_shipped_config_is_valid(name, fixture, answer) -> None:
    # The files a user copies must parse and reference real, installed plugins.
    d = _AGENTS_DIR / name
    cfg = load_config(str(d / "zu.yaml"))
    spec = load_task(str(d / "task.yaml"))
    assert spec.query and spec.output_schema  # the task is complete
    reg = build_registry(cfg)                  # every named plugin resolves
    for tool in cfg.plugins.tools:
        assert tool in reg.names("tools")
    for val in cfg.plugins.validators:
        assert val in reg.names("validators")
    assert (d / "fixtures" / fixture).is_file()


@pytest.mark.parametrize("name, fixture, answer", _AGENTS)
async def test_agent_runs_offline_and_grounds(agent_runner, name, fixture, answer) -> None:
    d = _AGENTS_DIR / name
    spec = load_task(str(d / "task.yaml"))
    html = (d / "fixtures" / fixture).read_text(encoding="utf-8")

    result, events = await agent_runner(
        [{"tool": "http_fetch", "args": {"url": spec.target}},
         {"text": json.dumps(answer), "finish": "stop"}],
        tools={"http_fetch": fetch_tool(text=html), "html_parse": HtmlParse()},
        validators={"schema": SchemaValidator(), "grounding": GroundingValidator()},
        spec=spec,
    )

    assert result.status is Status.SUCCESS          # schema + grounding both passed
    assert result.value == answer
    types = {e.type for e in events}
    assert {"harness.task.started", "data.source.fetched", "harness.task.completed"} <= types


@pytest.mark.parametrize("name, fixture, answer", _AGENTS)
async def test_fabricated_value_is_refused(agent_runner, name, fixture, answer) -> None:
    # A value NOT on the page must fail grounding — the run does not succeed.
    d = _AGENTS_DIR / name
    spec = load_task(str(d / "task.yaml"))
    html = (d / "fixtures" / fixture).read_text(encoding="utf-8")
    bogus = dict(answer)
    first = next(iter(bogus))
    bogus[first] = "totally-made-up-value-not-on-the-page"

    result, _events = await agent_runner(
        [{"tool": "http_fetch", "args": {"url": spec.target}},
         {"text": json.dumps(bogus), "finish": "stop"}],
        tools={"http_fetch": fetch_tool(text=html), "html_parse": HtmlParse()},
        validators={"schema": SchemaValidator(), "grounding": GroundingValidator()},
        spec=spec,
    )
    assert result.status is not Status.SUCCESS


def test_research_pipeline_example_runs_offline() -> None:
    # The multi-phase example (examples/agents/research-pipeline) runs end to end
    # with the scripted model — gated transitions, one replayable trace, no key.
    script = _AGENTS_DIR / "research-pipeline" / "pipeline.py"
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "status : success" in proc.stdout
    assert "one replayable log" in proc.stdout      # the whole pipeline is one trace
