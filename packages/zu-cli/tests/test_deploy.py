"""Phase 4: `zu deploy` — manifests and a local container."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from zu_cli import deploy
from zu_cli.main import app

runner = CliRunner()


def _seed_config(tmp_path):
    (tmp_path / "zu.yaml").write_text(
        "provider: { name: scripted }\nplugins: { validators: [schema] }\n", encoding="utf-8"
    )


def test_dockerfile_installs_zu_and_serves_without_baking_secrets():
    df = deploy.dockerfile_text("zu.yaml", extras="all", port=8000)
    assert 'pip install "zu-runtime[all]"' in df
    assert "zu" in df and "serve" in df and "COPY zu.yaml" in df
    # No secret is baked into the image: no ENV sets a key, no .env is copied.
    assert "ENV ANTHROPIC_API_KEY" not in df and "COPY .env" not in df


def test_compose_references_env_passthrough_not_values():
    txt = deploy.compose_text("zu-agent", "zu.yaml", port=8000)
    assert "ANTHROPIC_API_KEY" in txt and "8000:8000" in txt
    assert "=" not in txt.split("environment:")[1].split("restart:")[0]  # names only, no values


@pytest.mark.parametrize(
    "target,artifact",
    [("compose", "docker-compose.yml"), ("fly", "fly.toml"), ("render", "render.yaml"), ("dockerfile", "Dockerfile")],
)
def test_generate_writes_manifests(tmp_path, target, artifact):
    paths = deploy.generate(target, str(tmp_path), name="a", config="zu.yaml", extras="all", port=8000, force=True)
    assert any(p.endswith("Dockerfile") for p in paths)
    assert (tmp_path / artifact).exists()


def test_local_commands_pass_through_only_set_keys(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    build, run = deploy.local_commands("zu-agent", "zu.yaml", port=8000)
    assert build[:2] == ["docker", "build"]
    assert "ANTHROPIC_API_KEY" in run and "OPENAI_API_KEY" not in run
    assert "x" not in run  # the value is passed through by name, never embedded


def test_zu_deploy_compose_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_config(tmp_path)
    result = runner.invoke(app, ["deploy", "compose"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "docker-compose.yml").exists() and (tmp_path / "Dockerfile").exists()


def test_zu_deploy_local_dry_run_prints_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_config(tmp_path)
    result = runner.invoke(app, ["deploy", "local", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "docker build" in result.output and "docker run" in result.output
    assert (tmp_path / "Dockerfile").exists()


def test_zu_deploy_unknown_target_and_missing_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_config(tmp_path)
    assert runner.invoke(app, ["deploy", "nope"]).exit_code == 2
    monkeypatch.chdir(tmp_path / "..") if False else None
    # missing config
    import os
    os.remove(tmp_path / "zu.yaml")
    bad = runner.invoke(app, ["deploy", "compose"])
    assert bad.exit_code == 2 and "config error" in bad.output
