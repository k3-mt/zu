"""Phase 2: `zu init` + the shared scaffolder."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from zu_cli.config import load_agent
from zu_cli.main import app
from zu_cli.scaffold import TEMPLATE_NAMES, write_template

runner = CliRunner()


@pytest.mark.parametrize("template", TEMPLATE_NAMES)
def test_every_template_writes_a_loadable_agent(tmp_path, template):
    paths = write_template(str(tmp_path), template)
    assert {p.split("/")[-1] for p in paths} == {"agent.yaml"}
    # The scaffolded agent.yaml must actually parse into a (task, config).
    spec, cfg = load_agent(str(tmp_path / "agent.yaml"))
    assert cfg.provider.name and spec.query


def test_refuses_to_overwrite_without_force(tmp_path):
    write_template(str(tmp_path), "web")
    with pytest.raises(FileExistsError):
        write_template(str(tmp_path), "web")
    # force overwrites
    write_template(str(tmp_path), "minimal", force=True)


def test_zu_init_command(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--template", "minimal"])
    assert result.exit_code == 0, result.output
    assert "created" in result.output
    assert (tmp_path / "agent.yaml").exists()


def test_zu_init_unknown_template_rejected(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path), "--template", "nope"])
    assert result.exit_code == 2
    assert "unknown template" in result.output


def test_zu_init_no_clobber(tmp_path):
    runner.invoke(app, ["init", str(tmp_path), "--template", "web"])
    again = runner.invoke(app, ["init", str(tmp_path), "--template", "web"])
    assert again.exit_code == 1
    assert "refusing to overwrite" in again.output
