"""`zu run --offline`: drive a real example agent.yaml fully offline against its
captured fixtures/ bundle — scripted model, fixture-backed http_fetch and render_dom
— with no API key, no network, and no Docker. This is the CLI path for the cheap,
deterministic construction loop; the pytest in test_example_agents proves the same
loop at the library level, this proves it through the shipped command a user runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from zu_cli.config import ConfigError
from zu_cli.main import app
from zu_cli.offline import fixtures_dir_for, load_bundle

_AGENTS_DIR = Path(__file__).resolve().parents[3] / "examples" / "agents"


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch) -> None:
    # An offline run must need no key — prove it by removing any the env happens to
    # carry, so a regression that builds the live provider fails loudly here.
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_offline_tier1_price_extractor(tmp_path, monkeypatch) -> None:
    # cwd in a tmp dir so the default review-queue file (if any) never litters the repo.
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--offline", str(_AGENTS_DIR / "price-extractor")])
    assert result.exit_code == 0, result.output
    assert "status : success" in result.output
    assert "provider=scripted" in result.output
    assert "AeroPress Go Travel Coffee Press" in result.output
    assert "$39.95" in result.output


def test_offline_tier2_js_product_escalates_and_renders(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["run", "--offline", str(_AGENTS_DIR / "js-product")])
    assert result.exit_code == 0, result.output
    assert "status : success" in result.output
    # The expensive tier ran offline: the trace shows the escalation and the
    # tier-2 render_dom call driven by the FixtureBackend.
    assert "ESCALATE" in result.output
    assert "render_dom" in result.output
    assert "Acme Widget" in result.output
    assert "$9.00" in result.output


def test_offline_missing_bundle_is_a_clean_error() -> None:
    # An agent with no fixtures/ bundle fails with a clear ConfigError, not a traceback.
    with pytest.raises(ConfigError, match="fixtures"):
        load_bundle(fixtures_dir_for(str(_AGENTS_DIR / "custom-tool")))


def test_bundle_loads_fetch_and_render_maps() -> None:
    bundle = load_bundle(fixtures_dir_for(str(_AGENTS_DIR / "js-product")))
    url = "https://shop.example/p/acme-widget"
    # Same URL, different bodies per tier — the reason fetch/render are separate maps.
    assert "id=\"root\"" in bundle.fetch[url]["html"]
    assert "Acme Widget" in bundle.render[url]["html"]
    assert bundle.script[0]["tool"] == "http_fetch"
