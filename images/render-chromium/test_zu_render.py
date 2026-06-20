"""Unit tests for the zu-render entrypoint's GENERIC action dispatch.

The entrypoint is a script (not a package), so we load it by path. The browser is
not involved: ``_run_actions`` is driven against a fake ``page`` that records the
generic Playwright calls. This proves the entrypoint carries NO site-specific
logic — it just applies whatever click/fill/select/wait actions it is given, in
order, and reports the first failure while keeping the DOM captured so far.

    pytest images/render-chromium/test_zu_render.py
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

_loader = importlib.machinery.SourceFileLoader("zu_render", str(Path(__file__).parent / "zu-render"))
_spec = importlib.util.spec_from_loader("zu_render", _loader)
zr = importlib.util.module_from_spec(_spec)
_loader.exec_module(zr)


class _FakePage:
    """Records the generic browser ops _run_actions performs — no real browser."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_on = fail_on

    def _maybe_fail(self, sel: str) -> None:
        if self.fail_on is not None and sel == self.fail_on:
            raise RuntimeError("element not found")

    def click(self, sel, timeout=None):
        self.calls.append(("click", sel))
        self._maybe_fail(sel)

    def fill(self, sel, value, timeout=None):
        self.calls.append(("fill", sel, value))
        self._maybe_fail(sel)

    def select_option(self, sel, value, timeout=None):
        self.calls.append(("select", sel, value))
        self._maybe_fail(sel)

    def wait_for_selector(self, sel, timeout=None):
        self.calls.append(("wait_for", sel))
        self._maybe_fail(sel)

    def wait_for_timeout(self, ms):
        self.calls.append(("wait_ms", ms))


def test_actions_apply_in_order_generically() -> None:
    page = _FakePage()
    err = zr._run_actions(page, [
        {"click": "text=Next"},
        {"select": "#type", "value": "Consultation"},
        {"fill": "#q", "value": "dog"},
        {"wait_for": "text=Choose a time"},
        {"wait_ms": 500},
    ])
    assert err is None
    assert page.calls == [
        ("click", "text=Next"),
        ("select", "#type", "Consultation"),
        ("fill", "#q", "dog"),
        ("wait_for", "text=Choose a time"),
        ("wait_ms", 500),
    ]


def test_failed_action_is_reported_and_stops() -> None:
    page = _FakePage(fail_on="text=Missing")
    err = zr._run_actions(page, [{"click": "text=Next"}, {"click": "text=Missing"}, {"click": "text=After"}])
    assert err is not None and "text=Missing" in err
    assert ("click", "text=After") not in page.calls   # stopped at the failure


def test_unknown_action_is_reported() -> None:
    page = _FakePage()
    assert "unknown action" in zr._run_actions(page, [{"frobnicate": "x"}])


def test_action_list_is_capped() -> None:
    page = _FakePage()
    zr._run_actions(page, [{"wait_ms": 1} for _ in range(zr._MAX_ACTIONS + 10)])
    assert len(page.calls) == zr._MAX_ACTIONS    # a runaway action list is bounded
