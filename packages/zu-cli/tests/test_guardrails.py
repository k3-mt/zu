"""The anti-hardcode guardrail gate — the executable, $0 rules on autonomous output.

Proves each guardrail bites and clears: the minimal example trips G1 (a single-selector
step), a fixed bundle passes, an embedded answer trips G3, and an unreachable threshold
trips G2.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

from zu_cli.config import load_agent
from zu_cli.guardrails import enforce_guardrails
from zu_cli.offline import Bundle, bundle_path

_BROWSER_WIDGET = Path(__file__).resolve().parents[3] / "examples" / "agents" / "browser-widget"


def _bundle() -> Bundle:
    return Bundle.load(bundle_path(_BROWSER_WIDGET))


def _with_alternate_locators(bundle: Bundle) -> Bundle:
    """Add a `near` fallback to every browser targeting action — clears G1."""
    b = copy.deepcopy(bundle)
    for move in b.moves:
        if move.get("tool") == "browser" and move.get("args", {}).get("op") == "act":
            for action in move["args"].get("actions", []):
                if any(verb in action for verb in ("click", "fill", "select")):
                    action["near"] = "price"
    return b


async def test_g1_single_selector_violation_on_minimal_example() -> None:
    spec, cfg = load_agent(str(_BROWSER_WIDGET / "agent.yaml"))
    report = await enforce_guardrails(spec, cfg, _bundle(), _BROWSER_WIDGET)

    assert not report.passed
    assert any(v.rule == "single-selector" for v in report.violations)
    assert report.resilience == 1.0


async def test_guardrails_pass_with_alternate_locators(tmp_path: Path) -> None:
    d = tmp_path / "agent"
    shutil.copytree(_BROWSER_WIDGET, d, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))
    spec, cfg = load_agent(str(d / "agent.yaml"))
    report = await enforce_guardrails(spec, cfg, _with_alternate_locators(_bundle()), d)

    assert report.passed, [v.detail for v in report.violations]


async def test_g3_hardcoded_answer_violation(tmp_path: Path) -> None:
    # An agent.yaml that embeds a captured answer value verbatim must be refused.
    d = tmp_path / "agent"
    shutil.copytree(_BROWSER_WIDGET, d, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))
    (d / "agent.yaml").write_text(
        (d / "agent.yaml").read_text(encoding="utf-8") + '\n# cached answer: "$9.00"\n',
        encoding="utf-8")
    spec, cfg = load_agent(str(d / "agent.yaml"))

    report = await enforce_guardrails(spec, cfg, _with_alternate_locators(_bundle()), d)

    assert not report.passed
    assert any(v.rule == "hardcoded-answer" and "$9.00" in v.detail for v in report.violations)


async def test_g2_resilience_threshold_violation(tmp_path: Path) -> None:
    # An unreachable required resilience trips G2 (with G1 cleared so only G2 shows).
    d = tmp_path / "agent"
    shutil.copytree(_BROWSER_WIDGET, d, ignore=shutil.ignore_patterns("track.json", "cost.jsonl"))
    spec, cfg = load_agent(str(d / "agent.yaml"))

    report = await enforce_guardrails(
        spec, cfg, _with_alternate_locators(_bundle()), d, min_resilience=1.01)

    assert not report.passed
    assert any(v.rule == "resilience" for v in report.violations)
