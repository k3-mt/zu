"""RetrievalProvider reference impls — typed vendor/product discovery (#81).

``ModelProvider`` acts on a NAMED site; ``RetrievalProvider`` is its sibling for
"FIND the site". Both reference impls return ``Candidate`` records — typed FACTS,
never persuasive prose — so the planning model shortlists over a schema and the
content-free discipline holds through discovery.

Two impls:

* :class:`ScriptedRetrievalProvider` — the fake (a :class:`zu_providers.scripted`
  analog): replays canned candidates, no key, no network, deterministic. Almost
  every offline test of a discovery flow leans on it.
* :class:`WebSearchRetrievalProvider` — the FALLBACK the issue names: the existing
  ``web_search`` tool reduced to typed records. It carries only what search yields
  (title, url, domain); ``price``/``in_stock`` stay ``None`` (search doesn't know
  them — a structured shopping feed adapter fills them later). Its egress is the
  connector's single API host, so discovery cannot reach outside its allowlist.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from zu_core.ports import Candidate, RetrievalProvider, RetrievalQuery


def domain_of(url: str) -> str:
    """The registrable host of a URL, lowercased, ``www.`` stripped — the
    cacheable key a reputation check and a dedupe both want. Best-effort and
    pure (no PSL lookup): ``https://www.Shop.com/x`` -> ``shop.com``."""
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class ScriptedRetrievalProvider:
    """Replays a fixed list of candidates, ignoring the query — the fake model's
    discovery analog. Construct from :class:`Candidate` objects or terse dicts.

        ScriptedRetrievalProvider.from_records([
            {"title": "Red collar", "url": "https://shop.com/c", "price": 1299,
             "currency": "USD", "in_stock": True},
        ])

    ``limit`` on the query is honoured (a real provider returns at most ``limit``),
    so a test can exercise the cap deterministically."""

    name = "scripted"

    def __init__(self, candidates: list[Candidate]) -> None:
        self._candidates = list(candidates)

    @classmethod
    def from_records(cls, records: list[dict]) -> ScriptedRetrievalProvider:
        out: list[Candidate] = []
        for r in records:
            r = dict(r)
            # Derive the domain from the url when the record omits it — the same
            # convenience the web-search reducer applies, so both producers agree.
            if "domain" not in r and r.get("url"):
                r["domain"] = domain_of(r["url"])
            r.setdefault("source", cls.name)
            out.append(Candidate(**r))
        return cls(out)

    async def search(self, query: RetrievalQuery) -> list[Candidate]:
        return self._candidates[: max(0, query.limit)]


class WebSearchRetrievalProvider:
    """Reduce the existing ``web_search`` tool to typed candidates — the fallback
    when no structured shopping feed is wired (#81). It returns FACTS search can
    supply (title, url, derived domain); ``price``/``currency``/``in_stock`` stay
    ``None`` rather than being parsed out of prose (that would re-open the content
    hole discovery exists to avoid). A price-comparison feed adapter populates them.

    ``tool`` is any object with the ``web_search`` shape
    (``async __call__(ctx, query, num_results=None) -> {"results": [{title,url}]}``)
    and an ``egress`` — typically a :class:`zu_tools.search.WebSearch`. The egress
    is mirrored from the tool so discovery's allowlist is the connector's single
    host, not the open internet."""

    name = "web_search"

    def __init__(self, tool: Any) -> None:
        self._tool = tool
        # Mirror the search tool's scoped egress (its connector's API host), so a
        # discovery run declares the same fail-closed allowlist the tool does.
        self.egress: frozenset[str] = getattr(tool, "egress", frozenset())
        self.capabilities: frozenset[str] = getattr(tool, "capabilities", frozenset())

    async def search(self, query: RetrievalQuery) -> list[Candidate]:
        # ``ctx`` is unused by the search tool's body (it only calls its connector),
        # so a discovery-only caller need not stand up a full RunContext.
        out = await self._tool(None, query=query.text, num_results=query.limit)
        results = out.get("results", []) if isinstance(out, dict) else []
        candidates: list[Candidate] = []
        for r in results[: max(0, query.limit)]:
            url = r.get("url") or ""
            if not url:
                continue
            candidates.append(
                Candidate(
                    title=r.get("title") or "",
                    url=url,
                    domain=domain_of(url),
                    source=self.name,
                )
            )
        return candidates


# Structural conformance checks (no runtime cost; document intent).
_a: type[RetrievalProvider] = ScriptedRetrievalProvider
_b: type[RetrievalProvider] = WebSearchRetrievalProvider
