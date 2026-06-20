"""Factories that assemble the most-repeated test setups in one call.

These replace the hand-rolled ``Registry()`` + register loops (~30 sites), the
inline ``{"name": "scripted", ...}`` config dicts (5 sites), and the
``HttpFetch(allow_private=True, transport=MockTransport(...))`` pattern (4
packages). Heavy siblings are lazy-imported so importing this module only needs
zu-core."""

from __future__ import annotations

from typing import Any

from .doubles import mock_transport


def registry_with(
    *,
    tools: dict[str, Any] | None = None,
    detectors: dict[str, Any] | None = None,
    validators: dict[str, Any] | None = None,
) -> Any:
    """A fresh, ISOLATED ``Registry`` containing exactly the given plugins (name →
    instance) — never the process-wide one, so a test can't leak plugins into the
    next. Keeps each test self-contained."""
    from zu_core.registry import Registry

    reg = Registry()
    for kind, items in (("tools", tools), ("detectors", detectors), ("validators", validators)):
        for name, obj in (items or {}).items():
            reg.register(kind, name, obj)
    return reg


def fetch_tool(*, text: str = "", status: int = 200, allow_private: bool = True) -> Any:
    """The real ``http_fetch`` tool served a saved page off a mock transport — its
    SSRF/redirect/cap logic runs for real, only the network is faked. ``allow_private``
    defaults True so loopback test URLs skip DNS."""
    from zu_tools.fetch import HttpFetch

    return HttpFetch(allow_private=allow_private, transport=mock_transport(text=text, status=status))


def search_tool(results: list[dict] | None = None) -> Any:
    """The real ``web_search`` tool served canned results off a mock transport — its
    connector/result-parsing/grounding-content logic runs for real, only the search
    API is faked. ``results`` is a list of ``{"title", "url"}`` (defaults to one)."""
    import httpx

    from zu_tools.search import WebSearch

    items = results if results is not None else [{"title": "Result", "url": "https://example/"}]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": items})

    return WebSearch(api_key="test-key", transport=httpx.MockTransport(handler))


def scripted_config(
    moves: list[dict],
    *,
    tools: list[str] | None = None,
    detectors: list[str] | None = None,
    validators: list[str] | None = None,
    containment: str | None = None,
    **extra: Any,
) -> dict:
    """A ``RunConfig`` dict driven by the offline scripted provider — the shape the
    facade, CLI, server, and sandbox tests all need. ``moves`` is the scripted
    provider's move list; ``tools``/``detectors``/``validators`` name installed
    plugins to activate; ``extra`` overrides any top-level key."""
    cfg: dict = {"provider": {"name": "scripted", "script": list(moves)}}
    plugins = {
        k: v for k, v in (("tools", tools), ("detectors", detectors), ("validators", validators))
        if v is not None
    }
    if plugins:
        cfg["plugins"] = plugins
    if containment is not None:
        cfg["containment"] = containment
    cfg.update(extra)
    return cfg


def scripted_provider(moves: list[dict]) -> Any:
    """The offline ``ScriptedProvider`` from a list of fake moves."""
    from zu_providers.scripted import ScriptedProvider

    return ScriptedProvider.from_moves(moves)
