"""The `zu mcp` server — drive Zu from a coding agent (Claude Code, Cursor, …).

Exercises the tools/resources in-process via the FastMCP test API, offline (a
scripted provider), so no harness, no key, and no network are needed.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from zu_cli.mcp_server import build_server  # noqa: E402


def _result(out) -> dict:
    """call_tool returns content blocks (or (content, structured)); pull the
    JSON object the tool returned."""
    if isinstance(out, tuple):
        out = out[0]
    return json.loads(out[0].text)


async def test_lists_tools_and_resources():
    srv = build_server()
    tools = {t.name for t in await srv.list_tools()}
    assert {"zu_plugins", "zu_scaffold", "zu_validate", "zu_run", "zu_traces"} <= tools
    uris = {str(r.uri) for r in await srv.list_resources()}
    assert "zu://plugins" in uris and "zu://config/schema" in uris


async def test_zu_plugins_reports_discovered_plugins():
    srv = build_server()
    plugins = _result(await srv.call_tool("zu_plugins", {}))
    assert "scripted" in plugins["providers"]
    assert "schema" in plugins["validators"]


async def test_zu_scaffold_writes_starter_files(tmp_path):
    srv = build_server()
    res = _result(await srv.call_tool("zu_scaffold", {"directory": str(tmp_path), "template": "web"}))
    assert res["ok"] and len(res["files"]) == 2
    assert (tmp_path / "zu.yaml").exists() and (tmp_path / "task.yaml").exists()


async def test_zu_validate_ok_and_error(tmp_path):
    srv = build_server()
    await srv.call_tool("zu_scaffold", {"directory": str(tmp_path), "template": "web"})
    ok = _result(await srv.call_tool("zu_validate", {"config": str(tmp_path / "zu.yaml")}))
    assert ok["ok"] and ok["provider"] == "anthropic"
    assert "http_fetch" in ok["active_plugins"]["tools"]

    bad = _result(await srv.call_tool("zu_validate", {"config": {"provider": {"name": "nope"}}}))
    assert bad["ok"] is False and "unknown provider" in bad["error"]


async def test_zu_run_executes_and_persists_then_traces(tmp_path):
    srv = build_server()
    db = str(tmp_path / "mcp.db")
    cfg = {
        "provider": {"name": "scripted", "script": [{"text": '{"answer": "hi"}', "finish": "stop"}]},
        "plugins": {"validators": ["schema"]},
        "event_sink": {"driver": "sqlite", "path": db},
    }
    task = {
        "query": "q",
        "output_schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
    }

    run = _result(await srv.call_tool("zu_run", {"task": task, "config": cfg}))
    assert run["ok"] and run["status"] == "success"
    assert run["value"] == {"answer": "hi"}
    assert run["run_id"] and run["events"] >= 1

    # The run persisted to the configured sink; zu_traces reads it back.
    traces = _result(await srv.call_tool("zu_traces", {"db_path": db, "run_id": run["run_id"]}))
    assert traces["ok"] and traces["total"] >= 1
    types = [e["type"] for e in traces["events"]]
    assert "harness.task.completed" in types


async def test_zu_run_reports_model_failure_cleanly(tmp_path):
    srv = build_server()
    cfg = {
        "provider": {"name": "anthropic", "model": "claude-x", "api_key_env": "ZU_ABSENT_KEY"},
        "plugins": {"validators": ["schema"]},
    }
    task = {"query": "q", "output_schema": {"type": "object"}}
    run = _result(await srv.call_tool("zu_run", {"task": task, "config": cfg}))
    assert run["ok"] is False
    assert "ZU_ABSENT_KEY" in run["error"]
