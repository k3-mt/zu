"""web_search — the connector talks to one API host behind an injectable httpx
transport, so the tool is exercised hermetically with canned results and no key.

A MockTransport stands in for Exa; we lock the request shape (POST + x-api-key),
the parsed results, the scoped egress, and the `content` field that makes search
results count as grounding provenance.
"""

from __future__ import annotations

import json

import httpx
import pytest

from zu_tools.search import ExaConnector, WebSearch


def _exa_transport(results: list[dict], *, capture: dict | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["url"] = str(request.url)
            capture["api_key"] = request.headers.get("x-api-key")
            capture["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": results})

    return httpx.MockTransport(handler)


async def test_returns_title_url_results_and_groundable_content() -> None:
    tool = WebSearch(
        api_key="k",
        transport=_exa_transport([
            {"title": "Park Vets Chislehurst", "url": "https://parkvets.example/book"},
            {"title": "Other", "url": "https://other.example"},
        ]),
    )
    out = await tool(None, "park vets chislehurst booking")
    assert out["count"] == 2
    assert out["results"][0] == {
        "title": "Park Vets Chislehurst", "url": "https://parkvets.example/book"
    }
    # The chosen url is in `content`, so a url the agent later reports grounds
    # against what search returned (the loop stores `content` as source.fetched).
    assert "https://parkvets.example/book" in out["content"]


async def test_sends_post_with_api_key_and_query() -> None:
    cap: dict = {}
    tool = WebSearch(api_key="secret-key", num_results=3, transport=_exa_transport([], capture=cap))
    await tool(None, "q")
    assert cap["url"] == "https://api.exa.ai/search"
    assert cap["api_key"] == "secret-key"
    assert cap["body"] == {"query": "q", "numResults": 3}


async def test_per_call_num_results_overrides_default() -> None:
    cap: dict = {}
    tool = WebSearch(api_key="k", num_results=5, transport=_exa_transport([], capture=cap))
    await tool(None, "q", num_results=10)
    assert cap["body"]["numResults"] == 10


def test_egress_is_scoped_to_the_connector_host_not_open() -> None:
    # Unlike a page fetcher, search has a tight, allowlist-able egress.
    assert WebSearch(api_key="k").egress == frozenset({"api.exa.ai"})


def test_key_read_from_env_when_not_passed(monkeypatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "from-env")
    assert ExaConnector()._key() == "from-env"


def test_missing_key_is_a_clear_error(monkeypatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="no Exa API key"):
        ExaConnector()._key()


def test_unknown_connector_rejected() -> None:
    with pytest.raises(ValueError, match="unknown search connector"):
        WebSearch("bing")
