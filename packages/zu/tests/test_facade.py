"""The embed facade: `import zu` and run an agent in one line.

Proves the library entry point works offline (scripted provider, no key, no
network) from plain dicts and from files, returns the typed Result, exposes the
event log, and reuses one config across many runs via the Zu class.
"""

from __future__ import annotations

import json

import pytest

import zu
from zu import ConfigError, Status


def _cfg(answer: dict) -> dict:
    return {
        "provider": {"name": "scripted", "script": [{"text": json.dumps(answer), "finish": "stop"}]},
        "plugins": {"validators": ["schema"]},
    }


_TASK = {
    "query": "extract the product",
    "output_schema": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
        "required": ["name", "price"],
    },
}


def test_run_from_dicts_returns_typed_result():
    result = zu.run(_TASK, config=_cfg({"name": "Acme", "price": "$9"}))
    assert result.status is Status.SUCCESS
    assert result.value == {"name": "Acme", "price": "$9"}


def test_run_with_events_exposes_the_log():
    result, events = zu.run_with_events(_TASK, config=_cfg({"name": "Acme", "price": "$9"}))
    assert result.status is Status.SUCCESS
    assert events[-1].type == "harness.task.completed"
    assert any(e.type == "harness.task.started" for e in events)


def test_zu_class_reuses_one_config_for_many_runs():
    agent = zu.Zu(config=_cfg({"name": "Acme", "price": "$9"}))
    r1 = agent.run(_TASK)
    r2 = agent.run({**_TASK, "query": "again"})
    assert r1.status is Status.SUCCESS and r2.status is Status.SUCCESS


@pytest.mark.asyncio
async def test_async_entry_point():
    result = await zu.arun(_TASK, config=_cfg({"name": "Acme", "price": "$9"}))
    assert result.status is Status.SUCCESS


def test_run_from_files(tmp_path):
    cfg = tmp_path / "zu.yaml"
    cfg.write_text(
        "provider:\n  name: scripted\n"
        '  script: [{ text: \'{"name":"Acme","price":"$9"}\', finish: stop }]\n'
        "plugins:\n  validators: [schema]\n",
        encoding="utf-8",
    )
    task = tmp_path / "task.yaml"
    task.write_text(
        "query: extract\noutput_schema:\n  type: object\n"
        "  properties: { name: { type: string }, price: { type: string } }\n"
        "  required: [name, price]\n",
        encoding="utf-8",
    )
    result = zu.run(str(task), config=str(cfg))
    assert result.status is Status.SUCCESS


def test_task_inherits_config_budget():
    agent = zu.Zu(config={**_cfg({"name": "A", "price": "$1"}), "budget": {"max_steps": 3}})
    assert agent.config.budget.max_steps == 3


def test_bad_config_type_is_a_clean_error():
    with pytest.raises(ConfigError):
        zu.run(_TASK, config=12345)  # not a path/dict/RunConfig


def test_bad_task_is_a_clean_error():
    with pytest.raises(ConfigError):
        zu.run({"no_query": True}, config=_cfg({"x": "y"}))


def test_registration_decorators_are_exported():
    """The documented ``@zu.tool`` / ``@zu.detector`` / … surface resolves on the
    facade and registers onto the process-wide registry the loop reads."""
    from zu_core.registry import REGISTRY

    for name in ("tool", "detector", "validator", "provider", "backend", "sink"):
        assert callable(getattr(zu, name)), f"zu.{name} is not exported"

    @zu.tool
    class _FacadeProbeTool:
        name = "facade_probe_tool"

    assert "facade_probe_tool" in REGISTRY.names("tools")
