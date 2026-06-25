"""The plugin-gate contract case for the `patterns` kind + the boundary guard."""

from __future__ import annotations

from zu_patterns.cookie_banner import CookieBanner
from zu_patterns.login_form import LoginForm
from zu_redteam.contract import check_plugin


def test_conformant_pattern_has_no_findings() -> None:
    for pat in (CookieBanner(), LoginForm()):
        assert check_plugin("patterns", pat.name, pat) == []


def test_malformed_pattern_is_flagged() -> None:
    class Bad:
        name = "bad"
        # missing archetype, recognize, success_invariants, failure_invariants

    findings = check_plugin("patterns", "bad", Bad())
    details = " ".join(f.detail for f in findings)
    assert "archetype" in details
    assert "recognize" in details
    assert "success_invariants" in details
    assert "failure_invariants" in details


def test_zu_patterns_never_imports_zu_tools() -> None:
    """The BOUNDARY guard: zu-patterns speaks only the core SurfaceView. It must
    never import zu-tools — the one-way Surface → SurfaceView projection lives IN
    zu-tools. This locks the crux of the design.

    Static check (robust to whatever else the test session already imported): no
    zu_patterns module's source may reference ``zu_tools``."""
    import pathlib

    import zu_patterns

    root = pathlib.Path(zu_patterns.__path__[0])
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        text = py.read_text()
        if "zu_tools" in text or "zu-tools" in text:
            offenders.append(py.name)
    assert offenders == [], f"zu_patterns must not reference zu_tools, found in: {offenders}"
