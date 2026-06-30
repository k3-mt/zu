"""web_search — the connector talks to one API host behind an injectable httpx
transport, so the tool is exercised hermetically with canned results and no key.

A MockTransport stands in for Exa; we lock the request shape (POST + x-api-key),
the parsed results, the scoped egress, and the `content` field that makes search
results count as grounding provenance.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import httpx
import pytest

from zu_tools.search import ExaConnector, WebSearch

if TYPE_CHECKING:
    from zu_tools.search import SearchConnector


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


class _HostlessConnector:
    """A custom connector that violates the SearchConnector Protocol's ``host: str``
    contract by omitting ``host`` entirely. Protocols are not runtime-enforced, so the
    old code defaulted this to the open-egress wildcard ``"*"`` (== EGRESS_OPEN)."""

    async def search(self, query: str, num_results: int) -> list[dict[str, str]]:
        return []


class _StubConnector:
    """A well-formed custom connector: a single API ``host`` scopes its egress."""

    def __init__(self, host: str) -> None:
        self.host = host

    async def search(self, query: str, num_results: int) -> list[dict[str, str]]:
        return []


def test_hostless_connector_fails_closed_not_wildcard() -> None:
    # Issue #53: a custom connector with NO ``host`` attribute must FAIL CLOSED at
    # construction — never silently yield ``frozenset({"*"})`` (EGRESS_OPEN). This
    # asserts the wildcard fallback is gone (fails on the old getattr(..., "*")).
    # ``cast`` feeds an object that violates the Protocol's ``host: str`` contract,
    # exactly the runtime-only case the guard exists for (Protocols aren't enforced).
    with pytest.raises(ValueError, match="non-empty 'host'"):
        WebSearch(cast("SearchConnector", _HostlessConnector()))


def test_empty_string_host_fails_closed() -> None:
    # The attribute is present but falsy: not the wildcard, but a broken/empty
    # allowlist all the same. Rejected by the same non-truthy-host guard.
    with pytest.raises(ValueError, match="non-empty 'host'"):
        WebSearch(_StubConnector(host=""))


def test_none_host_fails_closed() -> None:
    # ``host=None`` likewise violates ``host: str`` — fed via cast to exercise the
    # runtime guard the type system cannot.
    with pytest.raises(ValueError, match="non-empty 'host'"):
        WebSearch(cast("SearchConnector", _StubConnector(host=cast("str", None))))


def test_wellformed_custom_connector_yields_single_host_egress() -> None:
    # A connector that honours the contract yields EXACTLY its single host — no
    # broadening, no wildcard.
    tool = WebSearch(_StubConnector(host="search.internal.example"))
    assert tool.egress == frozenset({"search.internal.example"})


def test_shipped_default_still_constructs_with_exa_host() -> None:
    # The shipped default (and connector="exa") still constructs and reports the
    # single Exa host — the fix does not regress the supported path.
    assert WebSearch(api_key="k").egress == frozenset({"api.exa.ai"})
    assert WebSearch(connector="exa", api_key="k").egress == frozenset({"api.exa.ai"})
