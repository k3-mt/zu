"""`zu test-plugin` — the CLI entry to the gate. We test the wiring (resolution,
exit codes), not the gate internals (covered in zu-redteam's own suite)."""

from __future__ import annotations

from typer.testing import CliRunner

import zu_cli.main as main
from zu_cli.main import app
from zu_redteam.fixtures import LeakyFetch, StaticFetch

runner = CliRunner()


def test_unknown_package_exits_2() -> None:
    res = runner.invoke(app, ["test-plugin", "definitely-not-installed-xyz"])
    assert res.exit_code == 2


def test_resolves_a_real_packages_plugins() -> None:
    # zu-checks ships both built-in detectors and validators, incl. the human-routing
    # captcha / human_gate detectors added with the HITL surface.
    plugins, _notes = main._resolve_package_plugins("zu-checks")
    names = {n for _k, n, _o in plugins}
    assert {"empty", "error", "js-shell", "bot-wall", "schema", "grounding"} <= names
    assert {"captcha", "human-gate"} <= names


def test_resolves_the_patterns_group() -> None:
    # Regression: the gate's discovery derives from the canonical registry.GROUPS, so a
    # newer plugin group (zu.patterns) is gateable — not a stale hardcoded subset that
    # silently makes zu-patterns invisible to `zu test-plugin`.
    plugins, _notes = main._resolve_package_plugins("zu-patterns")
    kinds = {k for k, _n, _o in plugins}
    names = {n for _k, n, _o in plugins}
    assert kinds == {"patterns"}
    assert {"login_form", "search_box", "cookie_banner"} <= names


def test_passes_a_safe_plugin(monkeypatch) -> None:
    monkeypatch.setattr(
        main, "_resolve_package_plugins",
        lambda pkg: ([("tools", "good_fetch", StaticFetch(name="good_fetch"))], []),
    )
    res = runner.invoke(app, ["test-plugin", "demo-safe", "--no-unit"])
    assert res.exit_code == 0, res.output
    assert "PASS" in res.output


def test_fails_an_unsafe_plugin(monkeypatch) -> None:
    monkeypatch.setattr(
        main, "_resolve_package_plugins",
        lambda pkg: ([("tools", "leaky_fetch", LeakyFetch())], []),
    )
    res = runner.invoke(app, ["test-plugin", "demo-leaky", "--no-unit"])
    assert res.exit_code == 1
    assert "FAIL" in res.output
