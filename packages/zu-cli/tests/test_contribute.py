"""Capability gaps → strong, reproducible issues (zu_report_gap / build_gap_report).

All $0 — building an issue reads files and renders markdown; nothing runs a model or a site.
"""

from __future__ import annotations

import json

import pytest

from zu_cli.contribute import GAP_LABEL, build_gap_report


def _agent_with_repro(tmp_path, *, with_bundle: bool = True):
    d = tmp_path / "agent"
    (d / "fixtures").mkdir(parents=True)
    (d / "agent.yaml").write_text(
        "provider: {name: scripted}\ntiers: {1: [http_fetch], 2: [browser]}\n", encoding="utf-8")
    if with_bundle:
        (d / "fixtures" / "capture.json").write_text('{"task": "x", "moves": []}', encoding="utf-8")
    return d


def test_gap_report_with_repro_embeds_config_and_repro(tmp_path):
    d = _agent_with_repro(tmp_path)
    r = build_gap_report(
        d, summary="browser can't resolve a control inside shadow DOM",
        expected="the click resolves the control by text",
        observed="action_error_kind soft; the element is never found",
        proposed="a shadow-DOM-piercing resolver in the robust-click fallback")

    assert r.has_repro and r.repro_path.endswith("fixtures/capture.json")
    assert r.title == "Capability gap: browser can't resolve a control inside shadow DOM"
    # the repeatable example + the offline repro command + the embedded agent.yaml
    assert "zu run <agent> --offline" in r.body
    assert "shadow DOM" in r.body and "action_error_kind" in r.body
    assert "tiers: {1: [http_fetch]" in r.body            # agent.yaml embedded
    assert "shadow-DOM-piercing resolver" in r.body       # the proposed generic capability


def test_gap_report_without_repro_nudges_to_capture(tmp_path):
    d = _agent_with_repro(tmp_path, with_bundle=False)
    r = build_gap_report(d, summary="x", expected="y", observed="z")
    assert not r.has_repro
    assert "No fixtures bundle attached" in r.body
    assert "zu_explore" in r.body or "zu capture" in r.body  # how to get a repro


def test_gh_command_uses_body_file_and_label(tmp_path):
    d = _agent_with_repro(tmp_path)
    r = build_gap_report(d, summary="s", expected="e", observed="o")
    cmd = r.gh_command("gap-report.md", repo="k3-mt/zu")
    assert cmd.startswith("gh issue create --repo k3-mt/zu")
    assert f"--label {GAP_LABEL}" in cmd and "--body-file gap-report.md" in cmd


async def test_mcp_zu_report_gap_writes_report_and_command(tmp_path):
    pytest.importorskip("mcp")
    from zu_cli.mcp_server import build_server

    d = _agent_with_repro(tmp_path)
    srv = build_server()
    tools = {t.name for t in await srv.list_tools()}
    assert "zu_report_gap" in tools
    uris = {str(r.uri) for r in await srv.list_resources()}
    assert "zu://contributing" in uris

    out = await srv.call_tool("zu_report_gap", {
        "agent": str(d), "summary": "detector won't escalate a lazy-loaded widget",
        "expected": "the embedded-widget detector escalates to tier 2",
        "observed": "stays tier 1; the widget never loads in the static HTML"})
    content = out[0] if isinstance(out, tuple) else out
    res = json.loads(content[0].text)

    assert res["ok"] and res["has_repro"]
    assert "gh issue create" in res["gh_command"] and GAP_LABEL in res["gh_command"]
    assert (d / "gap-report.md").is_file()
    assert "detector won't escalate" in res["issue_markdown"]
