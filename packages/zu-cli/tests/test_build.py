"""The construction spine — `zu build` chains the offline stages (build → record track
→ harden) into one gated run, at $0, and writes a hardened track.json. The live canary
and promotion are seams, not part of the spine.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from zu_cli.build import BuildReport, StageResult, build_offline
from zu_cli.config import load_agent
from zu_cli.offline import Bundle, bundle_path

_BROWSER_WIDGET = Path(__file__).resolve().parent / "agents" / "browser-widget"


def _copy_agent(tmp_path: Path) -> Path:
    dst = tmp_path / "agent"
    # Exclude runtime artifacts a local `zu run --offline`/`zu build` on the example
    # leaves behind (both gitignored): copying them in would seed the temp agent with a
    # stray track.json/cost.jsonl and break the "no track from a failed build" assertion.
    shutil.copytree(_BROWSER_WIDGET, dst, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))
    return dst


async def test_build_offline_chains_and_writes_hardened_track(tmp_path: Path) -> None:
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))

    report = await build_offline(spec, cfg, d, bundle)

    assert report.ok
    assert [s.name for s in report.stages] == ["build", "track", "harden"]
    assert all(s.status == "ok" for s in report.stages)
    # The deliverable: a hardened track.json next to the agent, projected from the run.
    assert report.track_path == str(d / "track.json")
    assert (d / "track.json").is_file()
    # The harden report rode along, scoring full resilience.
    assert report.harden is not None and report.harden.resilience == 1.0


async def test_build_holds_when_offline_run_fails(tmp_path: Path) -> None:
    # A bundle whose browser sequence runs short fails the offline build (stage 3); the
    # spine stops there and never records a track.
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))
    bundle.observations["browser"] = bundle.observations["browser"][:1]

    report = await build_offline(spec, cfg, d, bundle)

    assert not report.ok
    assert report.stages[0].name == "build" and report.stages[0].status == "failed"
    assert report.track_path is None
    assert not (d / "track.json").exists()      # no track from a failed build


async def test_build_holds_when_resilience_below_min_score(tmp_path: Path) -> None:
    # The track records (build succeeded), but an unreachable min-score holds promotion.
    d = _copy_agent(tmp_path)
    spec, cfg = load_agent(str(d / "agent.yaml"))
    bundle = Bundle.load(bundle_path(d))

    report = await build_offline(spec, cfg, d, bundle, min_score=1.01)

    assert not report.ok
    assert (d / "track.json").is_file()         # the track is written...
    harden_stage = next(s for s in report.stages if s.name == "harden")
    assert harden_stage.status == "failed"      # ...but harden held the gate


def test_build_report_ok_property() -> None:
    ok = BuildReport(stages=[StageResult("a", "ok", ""), StageResult("b", "skipped", "")])
    bad = BuildReport(stages=[StageResult("a", "ok", ""), StageResult("b", "failed", "")])
    assert ok.ok and not bad.ok


def test_build_cli_runs_and_writes_track(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from zu_cli.main import app

    d = _copy_agent(tmp_path)
    result = CliRunner().invoke(app, ["build", str(d)])
    assert result.exit_code == 0, result.output
    assert "hardened track ready" in result.output
    assert (d / "track.json").is_file()


def test_build_cli_with_canary_is_a_loud_seam(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from zu_cli.main import app

    d = _copy_agent(tmp_path)
    result = CliRunner().invoke(app, ["build", str(d), "--with-canary"])
    assert result.exit_code == 2
    assert "live lane" in result.output


def test_build_cli_without_bundle_is_clean_error(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from zu_cli.main import app

    (tmp_path / "agent.yaml").write_text(
        (_BROWSER_WIDGET / "agent.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    result = CliRunner().invoke(app, ["build", str(tmp_path)])
    assert result.exit_code == 2
    assert "zu capture" in result.output
