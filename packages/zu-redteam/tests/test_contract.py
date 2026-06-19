"""Gate 2 — port conformance checks catch a malformed plugin and pass a good one."""

from __future__ import annotations

from zu_redteam.contract import check_plugin
from zu_redteam.fixtures import NullDetector, PassValidator, StaticFetch


def test_good_plugins_conform() -> None:
    assert check_plugin("tools", "web_fetch", StaticFetch()) == []
    assert check_plugin("detectors", "null", NullDetector()) == []
    assert check_plugin("validators", "pass", PassValidator()) == []


def test_tool_without_envelope_is_flagged() -> None:
    class T:
        name = "t"
        tier = 1
        schema: dict = {"parameters": {"properties": {}}}
        prompt_fragment = "t()"
        async def __call__(self, ctx):  # pragma: no cover
            return {}

    findings = check_plugin("tools", "t", T())
    details = " ".join(f.detail for f in findings)
    assert "capabilities" in details and "egress" in details


def test_detector_missing_scope_is_flagged() -> None:
    class D:
        name = "d"
        def inspect(self, ctx):  # pragma: no cover
            return None

    findings = check_plugin("detectors", "d", D())
    assert any("scope" in f.detail for f in findings)
