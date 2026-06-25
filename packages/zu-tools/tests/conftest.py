"""Shared test fixtures for the zu-tools suite.

The browser-family tools (action_surface, browser, pointer, vision) share one
run-scoped session through a MODULE-LEVEL registry in ``zu_tools._session``
(keyed by run id) — that registry IS the cross-tool lookup the production wiring
relies on. Because it is process-global, a leaked entry from one test would be
visible to the next, so we reset it around every test for isolation.
"""

from __future__ import annotations

import pytest

from zu_tools import _session


@pytest.fixture(autouse=True)
def _clear_run_registry():
    """Reset the module-level run-scoped session registry before and after each
    test, so cross-tool sharing is proven from a clean slate every time."""
    with _session._LOCK:
        _session._RUNS.clear()
    yield
    with _session._LOCK:
        _session._RUNS.clear()
