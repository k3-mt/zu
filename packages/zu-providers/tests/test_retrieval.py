"""#81 — the RetrievalProvider reference impls, proved offline.

Discovery returns typed ``Candidate`` records (facts), so the planning model
shortlists over a schema and never reads persuasive page prose. The scripted fake
is deterministic and key-free; the web_search reducer turns the existing search
tool into the same typed records and mirrors its scoped egress.
"""

from __future__ import annotations

from zu_core.ports import Candidate, RetrievalProvider, RetrievalQuery
from zu_providers.retrieval import (
    ScriptedRetrievalProvider,
    WebSearchRetrievalProvider,
    domain_of,
)


def test_domain_of_strips_www_and_lowercases() -> None:
    assert domain_of("https://www.Shop.com/dogs/collar?x=1") == "shop.com"
    assert domain_of("http://Store.example.co/x") == "store.example.co"
    assert domain_of("not a url") == ""


async def test_scripted_returns_typed_candidates_and_derives_domain() -> None:
    p = ScriptedRetrievalProvider.from_records(
        [
            {"title": "Red collar", "url": "https://www.shop.com/c", "price": 1299,
             "currency": "USD", "in_stock": True},
            {"title": "Blue collar", "url": "https://pets.example/b", "price": 1599,
             "currency": "USD"},
        ]
    )
    out = await p.search(RetrievalQuery(text="dog collar"))
    assert all(isinstance(c, Candidate) for c in out)
    assert out[0].domain == "shop.com"  # derived from url
    assert out[0].price == 1299 and out[0].currency == "USD"  # price is minor units (int)
    assert out[0].in_stock is True
    assert out[1].in_stock is None  # absent fact stays None, never fabricated
    assert out[0].source == "scripted"  # provenance


async def test_scripted_honours_the_query_limit() -> None:
    p = ScriptedRetrievalProvider.from_records(
        [{"title": f"c{i}", "url": f"https://shop.com/{i}"} for i in range(5)]
    )
    assert len(await p.search(RetrievalQuery(text="x", limit=2))) == 2
    assert len(await p.search(RetrievalQuery(text="x", limit=0))) == 0


def test_candidate_is_frozen_and_hashable() -> None:
    c = Candidate(title="t", url="https://shop.com/x", domain="shop.com")
    assert c in {c}  # hashable
    try:
        c.title = "z"
    except Exception as exc:  # frozen → mutation refused
        assert "frozen" in str(exc).lower() or "instance" in str(exc).lower()
    else:
        raise AssertionError("Candidate should be frozen")


class _FakeSearch:
    """A web_search-shaped tool: returns canned {title,url} results, declares a
    scoped egress (its connector's host) just like zu_tools.search.WebSearch."""

    egress = frozenset({"api.exa.ai"})
    capabilities = frozenset({"net"})

    def __init__(self, results: list[dict]) -> None:
        self._results = results
        self.queries: list[str] = []

    async def __call__(self, ctx, query: str, num_results=None) -> dict:
        self.queries.append(query)
        return {"results": self._results, "count": len(self._results)}


async def test_web_search_reducer_yields_typed_candidates_and_mirrors_egress() -> None:
    tool = _FakeSearch(
        [
            {"title": "Collar A", "url": "https://www.shopA.com/a"},
            {"title": "Collar B", "url": "https://shopB.io/b"},
            {"title": "no url", "url": ""},  # dropped — a candidate needs a url
        ]
    )
    p = WebSearchRetrievalProvider(tool)
    # Egress is the search tool's scoped host, NOT the open internet.
    assert p.egress == frozenset({"api.exa.ai"})

    out = await p.search(RetrievalQuery(text="dog collar", limit=10))
    assert [c.domain for c in out] == ["shopa.com", "shopb.io"]
    assert tool.queries == ["dog collar"]
    # Search can't know price/stock — those stay None rather than parsed from prose.
    assert all(c.price is None and c.in_stock is None for c in out)
    assert all(c.source == "web_search" for c in out)


def test_both_impls_satisfy_the_port() -> None:
    assert isinstance(ScriptedRetrievalProvider([]), RetrievalProvider)
    assert isinstance(WebSearchRetrievalProvider(_FakeSearch([])), RetrievalProvider)
