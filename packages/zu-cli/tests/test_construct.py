"""The meta-agent construction loop — diagnose → edit → rebuild, proven offline.

A scripted strategist drives the loop to convergence (proving the orchestration without a
model); the live strategist and live capture are seams; the `zu construct` CLI exposes a
$0 readiness gate (`--check`) and the autonomous seam.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from zu_cli.config import load_agent
from zu_cli.construct import (
    Edit,
    LiveStrategist,
    ScriptedStrategist,
    construct,
    live_capture,
)
from zu_cli.offline import Bundle, bundle_path

_BROWSER_WIDGET = Path(__file__).resolve().parents[3] / "examples" / "agents" / "browser-widget"


def _copy_agent(tmp_path: Path) -> Path:
    d = tmp_path / "agent"
    shutil.copytree(_BROWSER_WIDGET, d)
    return d


def _with_alternate_locators(bundle: Bundle) -> Bundle:
    b = copy.deepcopy(bundle)
    for move in b.moves:
        if move.get("tool") == "browser" and move.get("args", {}).get("op") == "act":
            for action in move["args"].get("actions", []):
                if "click" in action:
                    action["near"] = "price"
    return b


async def test_loop_converges_with_scripted_strategist(tmp_path: Path) -> None:
    # The example trips G1 (single-selector). One scripted edit adds the alternate
    # locator; round 2 builds clean and clears the gate.
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))
    fix = Edit(bundle=_with_alternate_locators(bundle), note="add `near` fallback to the click")

    report = await construct(spec, cfg, d, bundle, ScriptedStrategist([fix]), max_rounds=3)

    assert report.converged
    assert len(report.rounds) == 2
    assert report.rounds[0].guardrails_passed is False   # round 1 held on G1
    assert report.rounds[1].note == "converged"


async def test_loop_gives_up_when_strategist_returns_none(tmp_path: Path) -> None:
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))

    report = await construct(spec, cfg, d, bundle, ScriptedStrategist([]), max_rounds=3)

    assert not report.converged
    assert len(report.rounds) == 1
    assert "gave up" in report.rounds[0].note


async def test_loop_respects_max_rounds(tmp_path: Path) -> None:
    # An edit that doesn't fix G1 (a no-op copy) never converges; the loop stops at the cap.
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))
    noop = Edit(bundle=copy.deepcopy(bundle), note="no-op")

    report = await construct(spec, cfg, d, bundle, ScriptedStrategist([noop, noop, noop]),
                             max_rounds=2)

    assert not report.converged
    assert len(report.rounds) == 2


async def test_live_strategist_and_capture_are_seams(tmp_path: Path) -> None:
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))

    # The autonomous loop hits the live-strategist seam on the first held round.
    with pytest.raises(NotImplementedError):
        await construct(spec, cfg, d, bundle, LiveStrategist(), max_rounds=2)
    # Live capture is a seam too.
    with pytest.raises(NotImplementedError):
        live_capture(spec, cfg, d)


def test_construct_cli_check_reports_not_ready(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from zu_cli.main import app

    d = _copy_agent(tmp_path)
    result = CliRunner().invoke(app, ["construct", str(d), "--check"])
    assert result.exit_code == 1, result.output
    assert "single-selector" in result.output
    assert "not ready" in result.output


def test_construct_cli_autonomous_hits_live_seam(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from zu_cli.main import app

    d = _copy_agent(tmp_path)
    result = CliRunner().invoke(app, ["construct", str(d)])
    assert result.exit_code == 2
    assert "live lane" in result.output


def test_construct_cli_without_bundle_is_clean_error(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from zu_cli.main import app

    (tmp_path / "agent.yaml").write_text(
        (_BROWSER_WIDGET / "agent.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    result = CliRunner().invoke(app, ["construct", str(tmp_path), "--check"])
    assert result.exit_code == 2
    assert "zu capture" in result.output
