"""A failed `zu run` offers to file a capability gap; `zu report-gap` is its CLI twin.

All $0 — no model, no network, no `gh` (the create path is gated off in every test)."""

from __future__ import annotations

import sys

from typer.testing import CliRunner

from zu_cli.main import _offer_gap_report, app
from zu_core.contracts import Result, Status


def _agent(tmp_path):
    d = tmp_path / "agent"
    (d / "fixtures").mkdir(parents=True)
    (d / "agent.yaml").write_text(
        "provider: {name: scripted}\ntiers: {1: [http_fetch]}\n", encoding="utf-8")
    (d / "fixtures" / "capture.json").write_text('{"task": "x", "moves": []}', encoding="utf-8")
    return d


def test_offer_non_tty_prints_hint_and_writes_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    d = _agent(tmp_path)

    _offer_gap_report(str(d), Result(status=Status.TERMINAL, reason="bot-wall"))

    err = capsys.readouterr().err
    assert "report-gap" in err and "bot-wall" in err          # the actionable hint
    assert not (d / "gap-report.md").exists()                 # never prompts / writes in CI


def test_offer_tty_yes_writes_report_without_gh(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
    monkeypatch.setattr("shutil.which", lambda name: None)    # no gh → never creates, stays hermetic
    d = _agent(tmp_path)

    _offer_gap_report(str(d), Result(status=Status.TERMINAL, reason="empty"))

    out = capsys.readouterr().out
    assert (d / "gap-report.md").is_file()
    assert "gh issue create" in out                          # prints the ready command instead
    assert "ended in terminal" in (d / "gap-report.md").read_text(encoding="utf-8")


def test_offer_tty_no_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
    d = _agent(tmp_path)

    _offer_gap_report(str(d), Result(status=Status.TERMINAL, reason="x"))

    assert not (d / "gap-report.md").exists()


def test_report_gap_command_writes_file_and_prints_command(tmp_path):
    d = _agent(tmp_path)
    res = CliRunner().invoke(app, [
        "report-gap", "--agent", str(d), "--no-create",
        "--summary", "render_dom can't pierce an iframe", "--observed", "the node is never seen"])

    assert res.exit_code == 0, res.output
    assert (d / "gap-report.md").is_file()
    assert "gh issue create" in res.output and "--label capability-gap" in res.output
